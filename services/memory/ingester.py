"""Pinard memory curated ingester service.

Pulls curated observations from Engram, embeds them via Rosetta, maps them to
the composed core+domain ontology, and upserts records + vectors into SurrealDB.

Also subscribes as a durable NATS consumer on `pinard.{vignoble}.memory.episodes`
for /teaching-episode extraction — normal mode uses prescribed types only;
teaching mode enables learned-type emergence.

LLM-availability handling (for teaching-episode extraction only):
- On startup: fetch token from MEMORY_TOKEN_URL, probe Anthropic API.
- If 401/403: log "LLM token expired", enter polling sleep (no message pull).
- Polling: 5m → 10m → 30m backoff; re-probe; transition to draining on success.
- Extraction failure: short backoff (10s → 30s → 60s), dead-letter after 5 attempts.

Status file: {VIGNOBLE_LOGS}/memory-ingester-status.json (updated each cycle).
Log file:    {VIGNOBLE_LOGS}/memory-ingester.log

Environment variables:
    NATS_URL            — NATS server URL (default: nats://localhost:4222)
    NATS_VIGNOBLE       — Vignoble name (required)
    VIGNOBLE_LOGS       — Path to vignoble logs dir (default: ./logs)
    SURREAL_URL         — SurrealDB endpoint
    SURREAL_USER        — SurrealDB root username
    SURREAL_PASS        — SurrealDB root password
    MEMORY_LLM_API          — Protocol adapter: ``openai-chat`` | ``anthropic-messages``
    MEMORY_LLM_BASE_URL     — Endpoint override
    MEMORY_LLM_MODEL        — Model id (overrides MEMORY_EXTRACTION_MODEL)
    MEMORY_LLM_AUTH         — Token source: ``google-sa`` | ``url`` | ``static-key``
    MEMORY_TOKEN_URL        — Pour-token URL (used when MEMORY_LLM_AUTH=url or auto)
    ANTHROPIC_API_KEY       — Direct Anthropic key (MEMORY_LLM_AUTH=static-key)
    MEMORY_EXTRACTION_MODEL — Extraction model (legacy; use MEMORY_LLM_MODEL)
    ENGRAM_URL          — Engram API URL (default: http://localhost:7437)
    ENGRAM_SINCE_HOURS  — Observation look-back window (default: 168)
    MEMORY_ENGRAM_SOURCE — 'http' (default, local dev) or 'postgres' (EKS / cloud RDS direct read)
    ENGRAM_PG_DSN       — Postgres DSN; required when MEMORY_ENGRAM_SOURCE=postgres

Admin flags (CLI):
    --reingest  Reset all ingest cursors to seq=0 and exit.  The next regular
                ingestion cycle re-reads every observation and re-derives entity
                roles using the current type_map.  Use after a type_map change
                to re-type an existing corpus without a manual cursor hack.
    --recurate  Delete all non-human wiki_doc rows and drop wiki_curator_cursor
                tables across every scope (vignes + vignoble-* + __global__ +
                optional parcelle), then exit.  The next regular startup
                regenerates all wiki pages cleanly.  Use after a curator/rollup
                change that alters wiki output so stale rows don't block dedup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nats
import nats.js
import nats.js.api

from .embeddings import EmbeddingError, embed
from .engram_reader import EngramReader
from .engram_postgres_reader import (
    CursorStore,
    EngramPostgresReader,
    EngramPostgresReaderError,
    list_projects as list_engram_projects,
)
from .llm_client import LLMAuthError, LLMClient
from .ontology.registry import OntologyRegistry
from .surrealdb.client import SCHEMA_PATH, SurrealClient, SurrealError
from .token_manager import LLMUnavailable, TokenManager

# ── Configuration ─────────────────────────────────────────────────────────────

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
VIGNOBLE = os.environ.get("NATS_VIGNOBLE", "default")
VIGNOBLE_LOGS = Path(os.environ.get("VIGNOBLE_LOGS", "./logs"))
# MEMORY_LLM_MODEL takes precedence; MEMORY_EXTRACTION_MODEL is the legacy fallback.
_llm_model_env = os.environ.get("MEMORY_LLM_MODEL", "")
EXTRACTION_MODEL = _llm_model_env or os.environ.get(
    "MEMORY_EXTRACTION_MODEL", "claude-haiku-4-5-20251001"
)

MEMORY_ENGRAM_SOURCE: str = os.environ.get("MEMORY_ENGRAM_SOURCE", "http")

STREAM_NAME = "pinard-memory"
CONSUMER_NAME = "pinard-memory-ingester"
EPISODES_SUBJECT = "pinard.*.memory.episodes"
RULES_SUBJECT = "pinard.*.memory.rules"
RULES_CONSUMER_NAME = "pinard-memory-rules"
MR_SUBJECT = "pinard.*.memory.mr"
MR_CONSUMER_NAME = "pinard-memory-mr"
DEAD_LETTER_SUBJECT = f"pinard.{VIGNOBLE}.memory.dead"
QUERY_SUBJECT = "pinard.*.query"

# Extraction-failure retry backoffs (seconds)
EXTRACTION_BACKOFF = [10, 30, 60]
MAX_EXTRACTION_ATTEMPTS = 5

# ── Logging setup ─────────────────────────────────────────────────────────────

VIGNOBLE_LOGS.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.RotatingFileHandler(
    VIGNOBLE_LOGS / "memory-ingester.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=3,
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
)

logger = logging.getLogger("pinard.memory.ingester")
logger.setLevel(logging.INFO)
logger.addHandler(_file_handler)
logger.addHandler(logging.StreamHandler())

# ── Status file ───────────────────────────────────────────────────────────────

STATUS_FILE = VIGNOBLE_LOGS / "memory-ingester-status.json"


# ── SurrealDB-backed cursor store ───────────────────────────────────────────

class SurrealCursorStore:
    """CursorStore backed by SurrealDB `ingest_cursor` table.

    Each project cursor is stored keyed by ``<source>:<project>`` so cursors are
    per-group_id and per-source, isolated within one SurrealDB database (which is
    already scoped per group_id).
    """

    def __init__(self, surreal: "SurrealClient", source: str = "engram_pg") -> None:
        self._surreal = surreal
        self._source = source

    def _key(self, project: str) -> str:
        return f"{self._source}:{project}"

    def get(self, project: str) -> int:
        return self._surreal.get_ingest_cursor(self._key(project))

    def update(self, project: str, seq: int) -> None:
        self._surreal.set_ingest_cursor(self._key(project), seq)


class _Status:
    def __init__(self) -> None:
        self.state: str = "idle"           # draining | waiting | idle
        self.llm_available: bool = False
        self.queue_depth: int = 0
        self.last_extraction_at: str | None = None
        self.extracted_today: int = 0
        self.errors_today: int = 0
        self.oldest_pending_age_hours: float = 0.0
        self._day_reset: str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _maybe_reset_day(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_reset:
            self.extracted_today = 0
            self.errors_today = 0
            self._day_reset = today

    def write(self) -> None:
        self._maybe_reset_day()
        data = {
            "state": self.state,
            "llm_available": self.llm_available,
            "queue_depth": self.queue_depth,
            "last_extraction_at": self.last_extraction_at,
            "extracted_today": self.extracted_today,
            "errors_today": self.errors_today,
            "oldest_pending_age_hours": self.oldest_pending_age_hours,
        }
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(STATUS_FILE)


# ── Observation → SurrealDB ingestion ─────────────────────────────────────────

def _observation_to_entity(obs_content: str, obs_type: str, registry: OntologyRegistry, group_id: str) -> tuple[str, str, str]:
    """Map an observation to (role, name, description) using the ontology.

    This is a lightweight heuristic mapping — full NER via LLM is reserved for
    teaching-episode extraction. For curated Engram observations the type is
    already known.
    """
    composed = registry.compose(group_id)
    # Map Engram obs_type to a core entity role.
    # Includes both the canonical ingester types and the mem_save observation types
    # used by the Pinard agent (bugfix, decision, architecture, discovery, pattern,
    # config, preference) which previously all fell through to "artifact".
    type_map = {
        # Canonical ingester types
        "rule": "decision",
        "fact": "artifact",
        "teaching-episode": "task",
        "summary": "task",
        "diagnosis": "diagnosis",
        "action": "action",
        "log_pattern": "log_pattern",
        "environment_condition": "environment_condition",
        "gate": "gate",
        "step": "step",
        "verdict": "verdict",
        # mem_save observation types from the Pinard agent
        "bugfix": "diagnosis",
        "decision": "decision",
        "architecture": "artifact",   # structural description, not an action item
        "discovery": "artifact",       # factual finding, not a decision
        "pattern": "decision",
        "config": "artifact",
        "preference": "artifact",
        # Previously unmapped types (fell through to "artifact")
        "session_summary": "task",     # lifecycle meta-record, not domain knowledge
        "plan": "task",                # actionable intent
        "manual": "decision",          # user-authored guidance = prescribed decision
    }
    role = type_map.get(obs_type, "artifact")
    # Validate role exists in composed ontology; fall back to artifact.
    if role not in composed.entity_roles():
        role = "artifact"

    # Use the first non-empty line as the name (most observations have a title
    # on the first line); fall back to a 120-char content truncation.
    # Strip leading markdown heading markers (## / #) so "## Goal" → "Goal".
    import re as _re
    first_line = obs_content.split("\n")[0].strip() if obs_content else ""
    first_line = _re.sub(r"^#+\s*", "", first_line).strip()
    name = (first_line or obs_content[:120].replace("\n", " ").strip())[:120]
    return role, name, obs_content


def ingest_observation(
    obs_content: str,
    obs_type: str,
    group_id: str,
    registry: OntologyRegistry,
    surreal: SurrealClient,
) -> None:
    """Embed and upsert one Engram observation into SurrealDB.

    If the resolved role is not in the composed ontology, the observation is
    routed to entity_staging (open-world classification) instead of entity.
    """
    role, name, description = _observation_to_entity(obs_content, obs_type, registry, group_id)
    try:
        vector = embed(obs_content)
    except EmbeddingError as exc:
        logger.warning("Rosetta embedding failed for observation: %s", exc)
        vector = None

    composed = registry.compose(group_id)
    if role in composed.entity_roles():
        surreal.upsert_entity(
            role=role,
            name=name,
            description=description,
            embedding=vector,
            provenance="engram_pg",
        )
    else:
        surreal.upsert_entity_staging(
            name=name,
            proposed_role=role,
            description=description,
            rationale=f"obs_type={obs_type!r} not in composed ontology for {group_id!r}",
            provenance="engram_observation",
            embedding=vector,
        )


# ── Teaching-episode extraction (LLM-backed) ──────────────────────────────────

def _extract_entities_from_episode(
    llm_client: LLMClient,
    episode_content: str,
    mode: str,
    group_id: str,
    registry: OntologyRegistry,
    surreal: SurrealClient,
) -> tuple[int, int]:
    """Extract entities/edges from an episode using the LLM.

    Returns (entities_extracted, edges_extracted).
    Raises LLMAuthError on 401/403.
    """
    composed = registry.compose(group_id)
    entity_roles = composed.entity_roles()
    edge_names = composed.edge_names()

    teaching_hint = (
        "This is a teaching session. You may identify novel entity types beyond "
        "the prescribed list if they represent genuinely new operational concepts. "
        "Mark novel types with ontology_tier=learned.\n"
        if mode == "teaching"
        else ""
    )

    prompt = (
        f"Extract operational knowledge entities and relationships from the following "
        f"agent conversation transcript.\n\n"
        f"Prescribed entity roles: {', '.join(entity_roles)}\n"
        f"Prescribed edge types: {', '.join(edge_names)}\n"
        f"{teaching_hint}\n"
        f"Return a JSON object with:\n"
        f'  "entities": [{{"role": str, "name": str, "description": str}}]\n'
        f'  "edges": [{{"from_role": str, "from_name": str, "relation": str, "to_role": str, "to_name": str, "description": str}}]\n\n'
        f"Transcript:\n{episode_content[:6000]}"
    )

    text = llm_client.complete(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    ) or "{}"
    # Extract JSON block from response.
    import re
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("LLM returned no JSON block; content: %s", text[:200])
        return 0, 0

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("LLM JSON parse error: %s", exc)
        return 0, 0

    entities = data.get("entities", [])
    edges = data.get("edges", [])

    known_roles = set(composed.entity_roles())
    # Build a set of known snake_case edge table names from composed edge types.
    import re as _re
    def _to_snake(n: str) -> str:
        s = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", n)
        s = _re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
        return s.lower()
    known_edge_tables = {_to_snake(cls.__name__) for cls in composed.edge_types}

    for ent in entities:
        role = ent.get("role", "artifact")
        name = ent.get("name", "")
        desc = ent.get("description", "")
        if not name:
            continue
        ontology_tier = ent.get("ontology_tier", "prescribed")
        try:
            vec = embed(f"{name}: {desc}")
        except EmbeddingError:
            vec = None
        if role in known_roles:
            surreal.upsert_entity(
                role=role,
                name=name,
                description=desc,
                data={"ontology_tier": ontology_tier},
                embedding=vec,
                provenance="episode_extraction",
            )
        else:
            # Unknown role — route to staging (open-world, not dropped).
            surreal.upsert_entity_staging(
                name=name,
                proposed_role=role,
                description=desc,
                rationale=f"ontology_tier={ontology_tier!r}: role not in composed ontology",
                provenance="episode_extraction",
                data={"ontology_tier": ontology_tier},
                embedding=vec,
            )

    for edge in edges:
        try:
            relation = edge.get("relation", "").lower().replace(" ", "_").replace("-", "_")
            if not relation:
                continue
            if relation in known_edge_tables:
                surreal.relate(
                    from_role=edge.get("from_role", ""),
                    from_name=edge.get("from_name", ""),
                    relation=relation,
                    to_role=edge.get("to_role", ""),
                    to_name=edge.get("to_name", ""),
                    description=edge.get("description", ""),
                )
            else:
                # Unknown relation — route to edge_staging.
                surreal.upsert_edge_staging(
                    from_name=edge.get("from_name", ""),
                    from_role=edge.get("from_role", ""),
                    to_name=edge.get("to_name", ""),
                    to_role=edge.get("to_role", ""),
                    proposed_relation=relation,
                    description=edge.get("description", ""),
                    rationale="relation not in composed ontology",
                    provenance="episode_extraction",
                )
        except SurrealError as exc:
            logger.debug("Edge relate failed (skipping): %s", exc)

    return len(entities), len(edges)


# ── MR knowledge ingestion (Pass 1: issue+MR → decisions) ─────────────────────

_MR_EXTRACTION_PROMPT = """\
You are given the original goal (one or more issues) and how it was realized (a merged MR).
Extract only **durable** knowledge as atomic entities:

- `decision` — a choice made and *why* ("chose X over Y because Z");
- `artifact` — a durable structural fact the change established;
- `diagnosis` — a root cause paired with its fix (issue: cause → MR: fix).

Capture **re-scope deltas**: if the change diverged from the issue’s stated intent, record it
("the issue assumed X; the change did Y because Z").

Ignore transient/process content: TODOs, “you forgot X”, renames, rebases, style nits, CI,
approvals, and restatements with no rationale.

If there is no durable decision — **or you are in any doubt** — **return no entities.**
A zero-entity result is expected and correct for trivial changes; prefer missing a marginal
item over capturing noise.

Issue context (what was intended):
{issues_text}

Merged MR (what was realized):
Title: {mr_title}
Description:
{mr_description}

Return a JSON object:
  {{"entities": [{{"role": str, "name": str, "description": str}}]}}
Entities must have role one of: decision, artifact, diagnosis.
Return no entities if there is nothing durable.
"""


def _extract_entities_from_mr(
    llm_client: "LLMClient",
    mr_payload: dict,
    group_id: str,
    registry: "OntologyRegistry",
    surreal: "SurrealClient",
) -> int:
    """Extract durable entities from an MR memory payload using an MR-specific prompt.

    Returns the count of entities written. Raises LLMAuthError on auth failure.
    Each entity is embedded and upserted with provenance="mr" as a single unit (no chunking).
    """
    import re as _re

    mr_title = mr_payload.get("title", "")
    mr_description = mr_payload.get("description", "")
    project = mr_payload.get("project", "")
    iid = mr_payload.get("iid", 0)
    mr_key = f"{project}!{iid}"
    issues = mr_payload.get("issues", [])
    files_changed = mr_payload.get("files_changed", [])
    merged_at = mr_payload.get("merged_at", "")
    url = mr_payload.get("url", "")

    issues_text = ""
    if issues:
        parts = []
        for iss in issues:
            iss_title = iss.get("title", "")
            iss_desc = iss.get("description", "")
            iid_n = iss.get("iid", "")
            parts.append(f"Issue #{iid_n}: {iss_title}\n{iss_desc[:2000]}")
        issues_text = "\n\n".join(parts)
    else:
        issues_text = "(no linked issues)"

    prompt = _MR_EXTRACTION_PROMPT.format(
        issues_text=issues_text[:4000],
        mr_title=mr_title,
        mr_description=mr_description[:4000],
    )

    text = llm_client.complete(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    ) or "{}"

    match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not match:
        logger.info("MR extraction: LLM returned no JSON for %s (zero entities)", mr_key)
        return 0

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("MR extraction: JSON parse error for %s: %s", mr_key, exc)
        return 0

    entities = data.get("entities", [])
    if not entities:
        return 0

    allowed_roles = {"decision", "artifact", "diagnosis"}
    issue_iids = [iss.get("iid") for iss in issues if iss.get("iid")]
    entity_data = {
        "mr": mr_key,
        "issues": issue_iids,
        "url": url,
        "merged_at": merged_at,
        "files_changed": files_changed,
    }

    count = 0
    for ent in entities:
        role = ent.get("role", "").strip()
        name = ent.get("name", "").strip()
        description = ent.get("description", "").strip()
        if not name or role not in allowed_roles:
            continue

        # Check for pre-existing entity with same (role, name) for supersession.
        existing = surreal.fetch_entity_by_role_name(role, name)
        existing_provenance = existing.get("provenance", "") if existing else ""

        # Embed description only (not files_changed — that stays in data).
        try:
            vector = embed(f"{name}: {description}")
        except EmbeddingError as exc:
            logger.warning("MR extraction: embedding failed for %s/%s: %s", mr_key, name, exc)
            vector = None

        surreal.upsert_entity(
            role=role,
            name=name,
            description=description,
            data=entity_data,
            embedding=vector,
            provenance="mr",
        )
        count += 1

        # Create supersedes edge if an older entity existed with a different provenance
        # or from a prior MR (same role+name = same deterministic id, so the old content
        # has been overwritten; we still create the edge to mark the supersession).
        if existing and existing_provenance and existing_provenance != "mr":
            # Prior entity was lesson/episode — record the supersession.
            try:
                surreal.query(
                    "RELATE (SELECT id FROM entity WHERE role = $role AND name = $name LIMIT 1)->"
                    "supersedes->(SELECT id FROM entity WHERE role = $role AND name = $name LIMIT 1) "
                    "SET description = $desc, data = $data",
                    {"role": role, "name": name, "desc": f"Superseded by MR {mr_key}", "data": {"mr": mr_key}},
                )
            except Exception as exc:
                logger.debug("MR extraction: supersedes self-relate skipped for %s: %s", name, exc)
        elif existing and existing_provenance == "mr":
            # Prior MR entity — create a proper supersedes edge using the raw SurrealDB relate.
            try:
                import hashlib as _hashlib
                rid = _hashlib.sha256(f"{role}\x00{name}".encode()).hexdigest()[:32]
                rec_id = f"entity:{rid}"
                surreal.relate(
                    from_role=role,
                    from_name=name,
                    relation="supersedes",
                    to_role=role,
                    to_name=name,
                    description=f"MR {mr_key} supersedes previous decision",
                    data={"mr": mr_key},
                )
            except Exception as exc:
                logger.debug("MR extraction: supersedes edge failed for %s: %s", name, exc)

    return count


def _handle_mr_sync(
    payload: dict,
    registry: "OntologyRegistry",
    token_manager: "TokenManager",
) -> str:
    """Synchronously handle an MR memory event.

    Returns "ok" on success (including zero entities), "llm_unavailable" when the LLM
    is not reachable (caller should nak+delay), or "error" on other failures.
    """
    project = payload.get("project", "").strip()
    iid = payload.get("iid", 0)
    group_id = payload.get("scope", project).strip() or project

    if not project or not iid or not group_id:
        logger.warning("MR ingester: malformed payload (missing project/iid/scope): %r", payload)
        return "ok"  # ack and discard

    mr_key = f"{project}!{iid}"

    try:
        with SurrealClient(group_id=group_id) as surreal:
            surreal.ensure_schema(registry=registry, group_id=group_id)

            # Idempotency: check if this MR has already been processed.
            cursor_store = SurrealCursorStore(surreal, source="mr")
            if cursor_store.get(mr_key) > 0:
                logger.info("MR ingester: %s already processed, skipping", mr_key)
                return "ok"

            # Acquire LLM client.
            try:
                llm_client = token_manager.get_client()
            except LLMUnavailable as exc:
                logger.warning("MR ingester: LLM unavailable for %s: %s", mr_key, exc)
                return "llm_unavailable"

            # Extract entities.
            try:
                n_entities = _extract_entities_from_mr(
                    llm_client=llm_client,
                    mr_payload=payload,
                    group_id=group_id,
                    registry=registry,
                    surreal=surreal,
                )
            except LLMAuthError as exc:
                logger.warning("MR ingester: LLM auth error for %s: %s", mr_key, exc)
                return "llm_unavailable"
            except Exception as exc:
                logger.error("MR ingester: extraction error for %s: %s", mr_key, exc)
                return "error"

            # Mark as processed (seq=1 = done).
            cursor_store.update(mr_key, 1)

            mr_title = payload.get("title", "")
            logger.info(
                "MR ingester: processed %s (%r) entities=%d",
                mr_key, mr_title[:60], n_entities,
            )
            return "ok"

    except SurrealError as exc:
        logger.error("MR ingester: SurrealDB error for %s: %s", mr_key, exc)
        return "error"


async def _run_mr_consumer(
    js: nats.js.JetStreamContext,
    registry: OntologyRegistry,
    token_manager: TokenManager,
) -> None:
    """Consume MR memory events from `pinard.*.memory.mr` (durable, all vignobles).

    Runs as a peer task alongside the episode and rules consumers.
    LLM-unavailable → nak(delay=300) to retry when the token is refreshed.
    """
    consumer = None
    sub_backoff = 5
    while consumer is None:
        try:
            consumer = await js.pull_subscribe(
                MR_SUBJECT,
                durable=MR_CONSUMER_NAME,
            )
        except Exception as exc:
            logger.warning(
                "MR consumer: failed to subscribe to %s: %s — retrying in %ss",
                MR_SUBJECT, exc, sub_backoff,
            )
            await asyncio.sleep(sub_backoff)
            sub_backoff = min(sub_backoff * 2, 300)

    logger.info("MR consumer listening on %s (durable=%s)", MR_SUBJECT, MR_CONSUMER_NAME)

    while True:
        try:
            msgs = await consumer.fetch(1, timeout=2)
        except nats.errors.TimeoutError:
            await asyncio.sleep(5)
            continue
        except Exception as exc:
            logger.error("MR consumer: NATS fetch error: %s", exc)
            await asyncio.sleep(5)
            continue

        for msg in msgs:
            try:
                payload = json.loads(msg.data.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("MR consumer: malformed payload: %s", exc)
                await msg.ack()
                continue

            result = await asyncio.to_thread(
                _handle_mr_sync, payload, registry, token_manager
            )

            if result == "ok":
                await msg.ack()
            elif result == "llm_unavailable":
                await msg.nak(delay=300)
            else:  # "error"
                attempts = int(msg.metadata.num_delivered or 1)
                if attempts >= MAX_EXTRACTION_ATTEMPTS:
                    logger.error(
                        "MR consumer: dead-lettering after %d attempts: %r",
                        attempts, payload.get("project"),
                    )
                    try:
                        nc = js._js._nc  # type: ignore[attr-defined]
                        await nc.publish(DEAD_LETTER_SUBJECT, msg.data)
                    except Exception as pub_exc:
                        logger.error("MR consumer: dead-letter publish failed: %s", pub_exc)
                    await msg.term()
                else:
                    backoff_idx = min(attempts - 1, len(EXTRACTION_BACKOFF) - 1)
                    await msg.nak(delay=EXTRACTION_BACKOFF[backoff_idx])


# ── NATS rules consumer (lesson pins — no LLM needed) ───────────────────────

def _process_rule_message_sync(
    payload: dict,
    registry: OntologyRegistry,
) -> None:
    """Direct upsert/replace/delete of a pinned lesson into SurrealDB. No LLM extraction.

    Supported ops (payload field ``op``):
      (absent / "upsert")  — pin a new lesson
      "replace"            — supersede an existing lesson entity
      "delete"             — delete an existing lesson entity
      "edit_entity"        — update description of any existing entity (sets manual_edit=true)

    Upsert/replace payload fields:
      title       (str)  — short label, used as entity name
      content     (str)  — full lesson text, used as description
      type        (str)  — observation type ("rule", "fact", …)
      project     (str)  — target scope / group_id
      confidence  (float)— optional, for future use

    Replace/delete additional fields:
      replaces    (str)  — SurrealDB record id of the entity to supersede (e.g. "entity:abc123")
      entity_id   (str)  — alias for replaces (delete uses entity_id)

    edit_entity fields:
      entity_id   (str)  — SurrealDB record id of the entity to edit (e.g. "entity:abc123")
      content     (str)  — new description text (re-embedded)
      project     (str)  — target scope / group_id
    """
    op = payload.get("op", "upsert").strip()

    # ── edit_entity op ───────────────────────────────────────────────────────
    if op == "edit_entity":
        entity_id = payload.get("entity_id", "").strip()
        content = payload.get("content", "").strip()
        group_id = payload.get("project", "").strip()
        if not entity_id or not content or not group_id:
            logger.warning("Skipping malformed edit_entity payload (missing entity_id/content/project): %r", payload)
            return
        with SurrealClient(group_id=group_id) as surreal:
            existing = surreal.fetch_entity_by_id(entity_id)
            if existing is None:
                logger.warning("edit_entity: entity not found id=%r scope=%s", entity_id, group_id)
                return
            try:
                vector = embed(content)
            except EmbeddingError as exc:
                logger.warning("Embedding failed for edit_entity id=%r: %s", entity_id, exc)
                vector = None
            updated = surreal.update_entity_description(entity_id, content, vector)
            if updated:
                logger.info(
                    "edit_entity: updated id=%r scope=%s role=%s name=%.60r",
                    entity_id, group_id,
                    updated.get("role", ""), updated.get("name", ""),
                )
            else:
                logger.warning("edit_entity: update returned no rows id=%r scope=%s", entity_id, group_id)
        return

    # ── delete op ────────────────────────────────────────────────────────────
    if op == "delete":
        entity_id = payload.get("entity_id", "").strip() or payload.get("replaces", "").strip()
        group_id = payload.get("project", "").strip()
        if not entity_id or not group_id:
            logger.warning("Skipping malformed delete payload (missing entity_id/project): %r", payload)
            return
        with SurrealClient(group_id=group_id) as surreal:
            existing = surreal.fetch_entity_by_id(entity_id)
            if existing is None:
                logger.warning("delete: entity not found id=%r scope=%s", entity_id, group_id)
                return
            if existing.get("provenance") != "lesson":
                logger.warning(
                    "delete: refusing to delete non-lesson entity id=%r provenance=%r",
                    entity_id, existing.get("provenance"),
                )
                return
            deleted = surreal.delete_entity_by_id(entity_id)
            if deleted:
                logger.info("Lesson deleted: scope=%s id=%r", group_id, entity_id)
            else:
                logger.warning("Lesson delete returned no rows: scope=%s id=%r", group_id, entity_id)
        return

    # ── replace op ───────────────────────────────────────────────────────────
    if op == "replace":
        replaces_id = payload.get("replaces", "").strip() or payload.get("entity_id", "").strip()
        group_id = payload.get("project", "").strip()
        title = payload.get("title", "").strip()
        content = payload.get("content", "").strip()
        obs_type = payload.get("type", "rule")
        if not replaces_id or not group_id or not title or not content:
            logger.warning("Skipping malformed replace payload (missing replaces/project/title/content): %r", payload)
            return
        with SurrealClient(group_id=group_id) as surreal:
            existing = surreal.fetch_entity_by_id(replaces_id)
            if existing is None:
                logger.warning("replace: entity not found id=%r scope=%s", replaces_id, group_id)
                return
            if existing.get("provenance") != "lesson":
                logger.warning(
                    "replace: refusing to supersede non-lesson entity id=%r provenance=%r",
                    replaces_id, existing.get("provenance"),
                )
                return
            deleted = surreal.delete_entity_by_id(replaces_id)
            if not deleted:
                logger.warning("replace: delete of old entity returned no rows id=%r scope=%s", replaces_id, group_id)
        # Fall through to upsert the new entity.
        role, name, description = _observation_to_entity(content, obs_type, registry, group_id)
        try:
            vector = embed(content)
        except EmbeddingError as exc:
            logger.warning("Embedding failed for lesson replace: %s", exc)
            vector = None
        with SurrealClient(group_id=group_id) as surreal:
            surreal.ensure_schema(registry=registry, group_id=group_id)
            composed = registry.compose(group_id)
            if role in composed.entity_roles():
                surreal.upsert_entity(
                    role=role,
                    name=name,
                    description=description,
                    embedding=vector,
                    provenance="lesson",
                )
                logger.info("Lesson replaced: scope=%s old=%r role=%s name=%.60r", group_id, replaces_id, role, name)
            else:
                surreal.upsert_entity_staging(
                    name=name,
                    proposed_role=role,
                    description=description,
                    rationale=f"obs_type={obs_type!r} not in composed ontology for {group_id!r}",
                    provenance="lesson",
                    embedding=vector,
                )
                logger.info("Lesson replace staged (unknown role): scope=%s old=%r role=%s name=%.60r", group_id, replaces_id, role, name)
        return

    # ── upsert op (default) ──────────────────────────────────────────────────
    title = payload.get("title", "").strip()
    content = payload.get("content", "").strip()
    obs_type = payload.get("type", "rule")
    group_id = payload.get("project", "").strip()

    if not title or not content or not group_id:
        logger.warning("Skipping malformed rule payload (missing title/content/project): %r", payload)
        return

    role, name, description = _observation_to_entity(content, obs_type, registry, group_id)
    try:
        vector = embed(content)
    except EmbeddingError as exc:
        logger.warning("Embedding failed for lesson rule: %s", exc)
        vector = None

    with SurrealClient(group_id=group_id) as surreal:
        surreal.ensure_schema(registry=registry, group_id=group_id)
        composed = registry.compose(group_id)
        if role in composed.entity_roles():
            surreal.upsert_entity(
                role=role,
                name=name,
                description=description,
                embedding=vector,
                provenance="lesson",
            )
            logger.info("Lesson rule upserted: scope=%s role=%s name=%.60r", group_id, role, name)
        else:
            surreal.upsert_entity_staging(
                name=name,
                proposed_role=role,
                description=description,
                rationale=f"obs_type={obs_type!r} not in composed ontology for {group_id!r}",
                provenance="lesson",
                embedding=vector,
            )
            logger.info("Lesson rule staged (unknown role): scope=%s role=%s name=%.60r", group_id, role, name)


async def _run_rules_consumer(
    js: nats.js.JetStreamContext,
    registry: OntologyRegistry,
) -> None:
    """Consume pinned lessons from `pinard.{vignoble}.memory.rules`.

    Each message is a JSON object with title/content/type/project fields.
    Processing is synchronous (embedding + SurrealDB upsert) with no LLM.
    Runs as a peer task alongside the episode consumer.
    """
    try:
        await js.find_stream(RULES_SUBJECT)
    except Exception:
        logger.warning("Stream for %s not found; rules consumer will wait for stream creation", RULES_SUBJECT)

    consumer = None
    sub_backoff = 5
    while consumer is None:
        try:
            consumer = await js.pull_subscribe(
                RULES_SUBJECT,
                durable=RULES_CONSUMER_NAME,
            )
        except Exception as exc:
            logger.warning(
                "Failed to subscribe to %s: %s — retrying in %ss",
                RULES_SUBJECT, exc, sub_backoff,
            )
            await asyncio.sleep(sub_backoff)
            sub_backoff = min(sub_backoff * 2, 300)

    logger.info("Rules consumer listening on %s (durable=%s, all vignobles)", RULES_SUBJECT, RULES_CONSUMER_NAME)

    while True:
        try:
            msgs = await consumer.fetch(1, timeout=2)
        except nats.errors.TimeoutError:
            await asyncio.sleep(5)
            continue
        except Exception as exc:
            logger.error("NATS fetch error on rules consumer: %s", exc)
            await asyncio.sleep(5)
            continue

        for msg in msgs:
            try:
                payload = json.loads(msg.data.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Malformed rules payload: %s", exc)
                await msg.ack()
                continue

            try:
                await asyncio.to_thread(_process_rule_message_sync, payload, registry)
                await msg.ack()
            except Exception as exc:
                logger.error("Failed to process rule message: %s — nacking", exc)
                await msg.nak(delay=30)


# ── NATS episode consumer ─────────────────────────────────────────────────────

async def _run_episode_consumer(
    js: nats.js.JetStreamContext,
    registry: OntologyRegistry,
    status: _Status,
    token_manager: TokenManager,
) -> None:
    """Consume episodes from `pinard.{vignoble}.memory.episodes`.

    Implements the full LLM-availability state machine:
      draining → process episodes
      waiting  → polling sleep (no message pull)
    """
    # Ensure durable consumer exists.
    try:
        await js.find_stream(EPISODES_SUBJECT)
    except Exception:
        logger.warning("Stream for %s not found; consumer will wait for stream creation", EPISODES_SUBJECT)

    # Subscribe with retry. The episodes path is SECONDARY (postgres backfill +
    # recall are the primary paths and run as independent tasks); a NATS/JetStream
    # hiccup here must NEVER terminate the process. Returning early used to end
    # run()'s final await and exit(0) → CrashLoopBackOff, taking backfill down too.
    consumer = None
    sub_backoff = 5
    while consumer is None:
        try:
            consumer = await js.pull_subscribe(
                EPISODES_SUBJECT,
                durable=CONSUMER_NAME,
            )
        except Exception as exc:
            logger.warning(
                "Failed to subscribe to %s: %s — retrying in %ss "
                "(backfill + recall unaffected)",
                EPISODES_SUBJECT,
                exc,
                sub_backoff,
            )
            await asyncio.sleep(sub_backoff)
            sub_backoff = min(sub_backoff * 2, 300)

    llm_client: LLMClient | None = None

    # Probe LLM on startup.
    try:
        llm_client = token_manager.get_client()
        status.llm_available = True
        status.state = "draining"
        logger.info("LLM available at startup; entering draining mode")
    except LLMUnavailable as exc:
        status.llm_available = False
        status.state = "waiting"
        logger.warning("LLM token expired at startup (%s); entering polling sleep", exc)

    status.write()

    while True:
        if not status.llm_available:
            # Polling sleep — do NOT pull messages.
            delay = token_manager.backoff_delay()
            status.write()
            await asyncio.sleep(delay)
            try:
                llm_client = token_manager.get_client()
                status.llm_available = True
                status.state = "draining"
                token_manager.reset_failures()
                logger.info("LLM now available; transitioning to draining mode")
            except LLMUnavailable as exc:
                logger.warning("LLM probe failed (%s); still waiting", exc)
            status.write()
            continue

        # Draining mode: pull one message at a time.
        try:
            msgs = await consumer.fetch(1, timeout=2)
        except nats.errors.TimeoutError:
            status.state = "idle"
            status.queue_depth = 0
            status.write()
            await asyncio.sleep(5)
            continue
        except Exception as exc:
            logger.error("NATS fetch error: %s", exc)
            await asyncio.sleep(5)
            continue

        for msg in msgs:
            await _process_episode_message(
                msg=msg,
                js=js,
                llm_client=llm_client,
                registry=registry,
                status=status,
                token_manager=token_manager,
            )
            # If LLM became unavailable during processing, stop pulling.
            if not status.llm_available:
                break


_EpisodeResult = tuple[str, int, int, int, str | None, Exception | None]
# (action, n_entities, n_edges, duration_ms, last_extraction_at, exc)
# action: "ack" | "nak" | "term" | "llm_auth_error"


def _do_process_episode_sync(
    llm_client: LLMClient,
    registry: OntologyRegistry,
    attempts: int,
    session_id: str,
    group_id: str,
    mode: str,
    content: str,
    turn_index: int,
) -> _EpisodeResult:
    """Synchronous episode processing body — runs in a thread via asyncio.to_thread.

    Returns (action, n_entities, n_edges, duration_ms, last_extraction_at, exc).
    """
    t0 = time.monotonic()
    try:
        with SurrealClient(group_id=group_id) as surreal:
            surreal.ensure_schema(registry=registry, group_id=group_id)
            n_entities, n_edges = _extract_entities_from_episode(
                llm_client=llm_client,
                episode_content=content,
                mode=mode,
                group_id=group_id,
                registry=registry,
                surreal=surreal,
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        last_extraction_at = datetime.now(tz=timezone.utc).isoformat()
        logger.info(
            "session=%s group=%s mode=%s turn=%d entities=%d edges=%d duration_ms=%d",
            session_id, group_id, mode, turn_index, n_entities, n_edges, duration_ms,
        )
        return ("ack", n_entities, n_edges, duration_ms, last_extraction_at, None)

    except LLMAuthError as exc:
        logger.warning("LLM token expired mid-drain (%s); entering polling sleep", exc)
        return ("llm_auth_error", 0, 0, 0, None, exc)

    except Exception as exc:
        if mode == "teaching" or not content.strip():
            logger.warning(
                "Extraction failed for session=%s (attempt %d/%d): %s — %s",
                session_id, attempts, MAX_EXTRACTION_ATTEMPTS,
                exc, content[:200],
            )
        return ("error", 0, 0, 0, None, exc)


async def _process_episode_message(
    msg: Any,
    js: nats.js.JetStreamContext,
    llm_client: LLMClient,
    registry: OntologyRegistry,
    status: _Status,
    token_manager: TokenManager,
) -> None:
    """Process a single episode NATS message."""

    try:
        payload = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Malformed episode payload: %s", exc)
        await msg.ack()
        return

    session_id = payload.get("session_id", "")
    group_id = payload.get("group_id", "default")
    mode = payload.get("mode", "normal")
    episode = payload.get("episode", {})
    content = episode.get("content", "")
    turn_index = episode.get("turn_index", 0)

    if not content:
        await msg.ack()
        return

    attempts = int((msg.metadata.num_delivered or 1))

    action, n_entities, n_edges, duration_ms, last_extraction_at, exc = (
        await asyncio.to_thread(
            _do_process_episode_sync,
            llm_client,
            registry,
            attempts,
            session_id,
            group_id,
            mode,
            content,
            turn_index,
        )
    )

    if action == "ack":
        status.extracted_today += 1
        status.last_extraction_at = last_extraction_at
        status.state = "draining"
        await msg.ack()

    elif action == "llm_auth_error":
        await msg.nak(delay=300)
        status.llm_available = False
        status.state = "waiting"
        token_manager._probe_failures = 0  # reset so backoff starts fresh

    else:  # "error"
        status.errors_today += 1
        if attempts >= MAX_EXTRACTION_ATTEMPTS:
            logger.error(
                "Dead-lettering episode session=%s after %d attempts: %s",
                session_id, attempts, exc,
            )
            try:
                nc = js._js._nc  # type: ignore[attr-defined]
                await nc.publish(DEAD_LETTER_SUBJECT, msg.data)
            except Exception as pub_exc:
                logger.error("Failed to publish dead-letter: %s", pub_exc)
            await msg.term()
        else:
            backoff_idx = min(attempts - 1, len(EXTRACTION_BACKOFF) - 1)
            delay = EXTRACTION_BACKOFF[backoff_idx]
            await msg.nak(delay=delay)

    status.write()


# ── Engram → SurrealDB curated ingestion loop ─────────────────────────────────

def _resolve_group_ids(registry: OntologyRegistry) -> list[str]:
    """Return the full list of group_ids to ingest.

    For ``MEMORY_ENGRAM_SOURCE=postgres``: fetch the live project list from
    ``cloud_mutations`` (all 24 projects), merged with any local registry
    entries and the ``WORKER_GROUP_ID`` default.

    For ``MEMORY_ENGRAM_SOURCE=http``: fall back to the registry + env default
    (original behaviour — no remote query).
    """
    default_group = os.environ.get("WORKER_GROUP_ID", "")
    base = set(registry.registered_groups())
    if default_group:
        base.add(default_group)

    if MEMORY_ENGRAM_SOURCE == "postgres":
        try:
            remote = list_engram_projects()
            logger.debug("Postgres project list: %d projects", len(remote))
            base.update(remote)
        except EngramPostgresReaderError as exc:
            logger.warning(
                "Could not fetch Postgres project list; falling back to local registry: %s", exc
            )

    return sorted(base)


def _run_engram_ingestion(registry: OntologyRegistry) -> None:
    """One-shot: pull curated Engram observations and upsert into SurrealDB.

    Source is selected by ``MEMORY_ENGRAM_SOURCE``:
    - ``http``     — local Engram HTTP API (default; local dev / non-EKS)
    - ``postgres`` — cloud Engram RDS via read-only ``engram_ro`` role (EKS)

    Called periodically from the main loop. Errors from the Engram reader are
    logged at ERROR level so they surface visibly — silent empty ingestion is
    dangerous for this write path. Transient network errors are WARNING.
    """
    from .engram_reader import EngramReaderError

    group_ids = _resolve_group_ids(registry)

    if not group_ids:
        logger.debug("No group_ids configured; skipping Engram ingestion")
        return

    logger.debug(
        "Engram ingestion: source=%s, %d project(s)", MEMORY_ENGRAM_SOURCE, len(group_ids)
    )

    for group_id in group_ids:
        _ingest_group(group_id, registry)


def _ingest_group(group_id: str, registry: OntologyRegistry) -> None:
    """Fetch + upsert observations for one group_id using the configured source."""
    from .engram_reader import EngramReaderError

    if MEMORY_ENGRAM_SOURCE == "postgres":
        # Single SurrealClient context: fetch (without advancing cursor) + upsert +
        # cursor advance — all in one connection so a upsert failure cannot leave
        # the cursor ahead of the data.
        try:
            with SurrealClient(group_id=group_id) as surreal:
                surreal.ensure_schema(registry=registry, group_id=group_id)
                cursor_store = SurrealCursorStore(surreal)
                reader_pg = EngramPostgresReader(
                    project=group_id,
                    cursor_store=cursor_store,
                    advance_cursor=False,
                )
                observations = reader_pg.fetch()

                if not observations:
                    logger.debug("No Engram observations for group %s", group_id)
                    return

                for obs in observations:
                    if not obs.content:
                        continue
                    try:
                        ingest_observation(
                            obs_content=obs.content,
                            obs_type=obs.obs_type,
                            group_id=group_id,
                            registry=registry,
                            surreal=surreal,
                        )
                    except SurrealError as exc:
                        logger.warning(
                            "SurrealDB upsert failed for obs %s: %s", obs.obs_id, exc
                        )

                # Advance cursor only after upsert loop completes without a
                # connection-level error.
                if reader_pg.max_seq > 0:
                    cursor_store.update(group_id, reader_pg.max_seq)

        except SurrealError as exc:
            logger.error("SurrealDB error for group %s: %s", group_id, exc)
            return
        except EngramPostgresReaderError as exc:
            msg = str(exc)
            if "query failed" in msg.lower() or "connect" in msg.lower():
                logger.warning(
                    "Engram Postgres transient error for group %s: %s", group_id, exc
                )
            else:
                logger.error(
                    "Engram Postgres ingestion error for group %s: %s", group_id, exc
                )
            return
    else:
        # NOTE: This HTTP fallback path is a last resort for non-EKS environments where
        # Postgres is unavailable. In EKS the Postgres path is always used; this branch
        # should never be reached in production and will 404 on the Engram HTTP API.
        try:
            reader_http = EngramReader(group_id=group_id)
            observations = reader_http.fetch()
        except EngramReaderError as exc:
            msg = str(exc)
            if "connection failed" in msg.lower() or "timeout" in msg.lower():
                logger.warning(
                    "Engram transient connection error for group %s: %s", group_id, exc
                )
            else:
                logger.error(
                    "Engram ingestion error for group %s (possible misconfiguration): %s",
                    group_id, exc,
                )
            return

        if not observations:
            logger.debug("No Engram observations for group %s", group_id)
            return

        try:
            with SurrealClient(group_id=group_id) as surreal:
                surreal.ensure_schema(registry=registry, group_id=group_id)
                for obs in observations:
                    if not obs.content:
                        continue
                    try:
                        ingest_observation(
                            obs_content=obs.content,
                            obs_type=obs.obs_type,
                            group_id=group_id,
                            registry=registry,
                            surreal=surreal,
                        )
                    except SurrealError as exc:
                        logger.warning(
                            "SurrealDB upsert failed for obs %s: %s", obs.obs_id, exc
                        )
        except SurrealError as exc:
            logger.error("SurrealDB connection failed for group %s: %s", group_id, exc)


# ── NATS query handler (memory.query request-reply) ───────────────────────────

async def _run_query_handler(nc: Any, registry: OntologyRegistry) -> None:
    """Subscribe to memory.query and respond with recipe facts."""
    from .query_handler import handle_query_message

    async def _cb(msg: Any) -> None:
        await handle_query_message(msg, registry)

    await nc.subscribe(QUERY_SUBJECT, cb=_cb)
    logger.info("Query handler listening on %s", QUERY_SUBJECT)


# ── Main entrypoint ───────────────────────────────────────────────────────────

async def run(registry: OntologyRegistry | None = None) -> None:
    """Main ingester coroutine. Connects to NATS and starts all loops."""
    if registry is None:
        registry = OntologyRegistry()

    status = _Status()
    token_manager = TokenManager()

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    logger.info(
        "Pinard memory ingester starting — vignoble=%s nats=%s (serving all vignobles via wildcard)",
        VIGNOBLE, NATS_URL,
    )

    # Start query handler (non-blocking subscription).
    await _run_query_handler(nc, registry)

    # Start mid-session recall service (non-blocking subscription).
    from .recall_service import start_recall_subscription, start_boot_recall_subscription
    await start_recall_subscription(nc)

    # Start boot-recall service (hierarchical scope injection at spawn).
    await start_boot_recall_subscription(nc)

    # Engram ingestion: run once at startup, then every 30 minutes.
    async def _engram_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(_run_engram_ingestion, registry)
            except Exception as exc:
                logger.error("Engram ingestion loop error: %s", exc)
            await asyncio.sleep(30 * 60)

    asyncio.create_task(_engram_loop())

    # Scope roll-up + promotion: run once at startup, then every 2 hours.
    async def _rollup_loop() -> None:
        from .rollup import ScopeRollupEngine
        from .promotion import PromotionCandidateDetector
        from .obsidian_promoter import write_candidates
        from .rollup import (
            _load_all_vignoble_memberships,
            _get_vignobles_base_dir,
        )
        from .embeddings import embed
        from .llm_client import build_llm_client

        engine = ScopeRollupEngine(
            llm_client=build_llm_client(),
            embed_fn=embed,
        )

        def _do_rollup() -> None:
            engine.run()

        def _do_promotion() -> None:
            from .rollup import _vignoble_db, _parcelle_db
            vignobles_base = _get_vignobles_base_dir()
            membership = _load_all_vignoble_memberships(vignobles_base) if vignobles_base else {}
            all_vignoble_dbs = [
                _vignoble_db(vname) for vname in membership
            ]
            parcelle = os.environ.get("PINARD_PARCELLE", "").strip()
            if parcelle:
                all_vignoble_dbs.append(_parcelle_db(parcelle))
            if all_vignoble_dbs:
                detector = PromotionCandidateDetector(
                    vignoble_scopes=all_vignoble_dbs
                )
                candidates = detector.detect(obs_types=["rule", "fact"])
                if candidates:
                    write_candidates(candidates)
                    logger.info(
                        "Wrote %d promotion candidate(s) to Obsidian vault",
                        len(candidates),
                    )

        while True:
            try:
                await asyncio.to_thread(_do_rollup)
            except Exception as exc:
                logger.error("Rollup engine error: %s", exc)

            try:
                await asyncio.to_thread(_do_promotion)
            except Exception as exc:
                logger.error("Promotion detection error: %s", exc)

            await asyncio.sleep(2 * 60 * 60)

    asyncio.create_task(_rollup_loop())

    # Wiki curator + inbound sync: run once at startup, then every 6 hours.
    async def _wiki_curator_loop() -> None:
        from .wiki.curator import curate_all_vignobles, sync_out_vignoble_shared
        from .wiki.sync_in import sync_all_vignobles
        from .embeddings import embed
        from .llm_client import build_llm_client
        from .rollup import _get_vignobles_base_dir

        vignobles_base = _get_vignobles_base_dir()
        global_wiki_root = os.environ.get("GLOBAL_WIKI_ROOT", "")

        if not vignobles_base:
            logger.debug("VIGNOBLES_BASE_DIR not set — wiki curator loop inactive")
            return

        llm = build_llm_client()

        # GitLab project path for wiki MR creation (e.g. "your-group/pinard-wiki").
        gitlab_wiki_repo = os.environ.get("GITLAB_WIKI_REPO", "")

        def _do_curate() -> None:
            try:
                results = curate_all_vignobles(
                    vignobles_base_dir=vignobles_base,
                    global_wiki_root=global_wiki_root or None,
                    embed_fn=embed,
                    llm_client=llm,
                    registry=registry,
                    gitlab_repo=gitlab_wiki_repo,
                )
                logger.info("Wiki curator complete: %s", results)
            except Exception as exc:
                logger.error("Wiki curator loop error: %s", exc)

        def _do_sync_out_shared() -> None:
            """Write vignoble-scoped wiki_doc rows (from curate-on-promote) to wiki/_shared/.

            Must run after _do_curate so that _vignoble_db auto_serve rows exist.
            """
            try:
                results = sync_out_vignoble_shared(
                    vignobles_base_dir=vignobles_base,
                    embed_fn=embed,
                    gitlab_repo=gitlab_wiki_repo,
                )
                logger.info("Wiki vignoble-shared sync-out complete: %s", results)
            except Exception as exc:
                logger.error("Wiki vignoble-shared sync-out loop error: %s", exc)

        def _do_sync_in() -> None:
            try:
                results = sync_all_vignobles(
                    vignobles_base_dir=vignobles_base,
                    global_wiki_root=global_wiki_root or None,
                    embed_fn=embed,
                    registry=registry,
                )
                logger.info("Wiki inbound sync complete: %s", results)
            except Exception as exc:
                logger.error("Wiki inbound sync loop error: %s", exc)

        while True:
            await asyncio.to_thread(_do_curate)
            await asyncio.to_thread(_do_sync_out_shared)
            await asyncio.to_thread(_do_sync_in)
            await asyncio.sleep(6 * 60 * 60)

    asyncio.create_task(_wiki_curator_loop())

    # Rules consumer (lesson pins — no LLM, runs as a peer task).
    asyncio.create_task(_run_rules_consumer(js, registry))

    # MR memory consumer (Pass 1: issue+MR → decisions, runs as a peer task).
    asyncio.create_task(_run_mr_consumer(js, registry, token_manager))

    # Episode consumer (blocking).
    await _run_episode_consumer(js, registry, status, token_manager)


def _validate_env() -> None:
    """Fail fast if required env vars are missing or invalid."""
    vignobles_base = os.environ.get("VIGNOBLES_BASE_DIR", "").strip()
    if not vignobles_base:
        sys.stderr.write(
            "FATAL: VIGNOBLES_BASE_DIR is required (parent dir of vignoble clones, "
            "e.g. /data/repos/vignobles) — memory service cannot start\n"
        )
        sys.exit(1)
    from pathlib import Path
    if not Path(vignobles_base).is_dir():
        sys.stderr.write(
            f"FATAL: VIGNOBLES_BASE_DIR={vignobles_base!r} does not exist or is not a directory "
            "— memory service cannot start\n"
        )
        sys.exit(1)


def _reset_cursors_for_reingest(registry: OntologyRegistry) -> None:
    """Reset ingest cursors to 0 for all configured group_ids.

    The next regular ingestion cycle will re-read all observations from seq=0
    and re-derive roles using the current type_map.  Because ``upsert_entity``
    is content-addressed (sha256(role+name)), observations whose role changes
    produce a new entity record; the old artifact-typed records become orphaned
    until a future schema prune.
    """
    group_ids = _resolve_group_ids(registry)
    if not group_ids:
        logger.warning("--reingest: no group_ids configured, nothing to reset")
        return
    source = "engram_pg"
    for group_id in group_ids:
        try:
            with SurrealClient(group_id=group_id) as surreal:
                surreal.set_ingest_cursor(f"{source}:{group_id}", 0)
                logger.info("--reingest: reset cursor for group=%s to seq=0", group_id)
        except SurrealError as exc:
            logger.error("--reingest: failed to reset cursor for group=%s: %s", group_id, exc)


def _recurate_all_scopes(registry: OntologyRegistry) -> None:
    """Delete non-human wiki_doc rows and drop wiki_curator_cursor across all scopes.

    Covers every scope that the wiki curator writes to:
    - vignes (per-project group_ids from _resolve_group_ids)
    - vignoble-* scopes (from VIGNOBLES_BASE_DIR)
    - __global__ scope (GLOBAL_WIKI_GROUP)
    - parcelle-* scope if PINARD_PARCELLE is set

    Best-effort: a failure for one scope is logged and does not abort the rest.
    """
    from .rollup import (
        _load_all_vignoble_memberships,
        _get_vignobles_base_dir,
        _vignoble_db,
        _parcelle_db,
    )
    from .wiki.curator import GLOBAL_WIKI_GROUP

    # Build the full set of scope DB names.
    scopes: set[str] = set()

    # Vignes (per-project).
    scopes.update(_resolve_group_ids(registry))

    # Vignoble-scoped DBs.
    vignobles_base = _get_vignobles_base_dir()
    if vignobles_base:
        membership = _load_all_vignoble_memberships(vignobles_base)
        for vname in membership:
            scopes.add(_vignoble_db(vname))

    # Global scope.
    scopes.add(GLOBAL_WIKI_GROUP)

    # Parcelle scope (optional).
    parcelle = os.environ.get("PINARD_PARCELLE", "").strip()
    if parcelle:
        scopes.add(_parcelle_db(parcelle))

    logger.info("--recurate: clearing %d scope(s)", len(scopes))

    for scope in sorted(scopes):
        try:
            with SurrealClient(group_id=scope) as surreal:
                deleted = surreal.query(
                    "DELETE wiki_doc WHERE frontmatter.source != 'human'"
                    " OR frontmatter.source IS NONE"
                )
                n_deleted = len(deleted[0]) if deleted and deleted[0] else 0
                surreal.query("REMOVE TABLE IF EXISTS wiki_curator_cursor")
                logger.info(
                    "--recurate: scope=%s deleted %d wiki_doc row(s), cursor table dropped",
                    scope, n_deleted,
                )
        except SurrealError as exc:
            logger.error("--recurate: failed for scope=%s: %s", scope, exc)


def _rechunk_all_scopes(registry: OntologyRegistry) -> None:
    """Backfill wiki_chunk rows for all existing wiki_doc pages across all scopes.

    For every scope: iterate all wiki_doc rows, delete existing chunks for that
    path, derive chunks from the stored body, embed each chunk, and upsert.
    Idempotent; safe to re-run when the chunking strategy or embedding model changes.

    Mirrors the scope set used by _recurate_all_scopes.
    """
    from .rollup import (
        _load_all_vignoble_memberships,
        _get_vignobles_base_dir,
        _vignoble_db,
        _parcelle_db,
    )
    from .wiki.curator import GLOBAL_WIKI_GROUP
    from .wiki.sync_in import chunk_body as _wiki_chunk_body

    scopes: set[str] = set()
    scopes.update(_resolve_group_ids(registry))

    vignobles_base = _get_vignobles_base_dir()
    if vignobles_base:
        membership = _load_all_vignoble_memberships(vignobles_base)
        for vname in membership:
            scopes.add(_vignoble_db(vname))

    scopes.add(GLOBAL_WIKI_GROUP)

    parcelle = os.environ.get("PINARD_PARCELLE", "").strip()
    if parcelle:
        scopes.add(_parcelle_db(parcelle))

    logger.info("--rechunk: processing %d scope(s)", len(scopes))

    for scope in sorted(scopes):
        try:
            with SurrealClient(group_id=scope) as surreal:
                surreal.ensure_schema(registry=registry, group_id=scope)
                pages = surreal.query(
                    "SELECT path, title, body FROM wiki_doc WHERE body != '' LIMIT 10000"
                )
                page_list = pages[0] if pages and isinstance(pages[0], list) else []
                total = len(page_list)
                logger.info("--rechunk: scope=%s pages=%d", scope, total)

                chunked = 0
                errors = 0
                for page in page_list:
                    path = page.get("path", "")
                    title = page.get("title", path)
                    body = page.get("body", "")
                    if not path or not body:
                        continue
                    try:
                        # Reuse WikiSyncer's chunking logic (no embed_fn needed at construction).
                        chunks = _wiki_chunk_body(title, body)
                        surreal.delete_wiki_chunks_by_path(path)
                        embedded_chunks = []
                        for chunk in chunks:
                            chunk_embedding = None
                            try:
                                chunk_embedding = embed(chunk["embed_text"])
                            except EmbeddingError as exc:
                                logger.debug("Embed failed for %s chunk %d: %s", path, chunk["chunk_index"], exc)
                            embedded_chunks.append({
                                "parent_path": path,
                                "heading": chunk["heading"],
                                "chunk_index": chunk["chunk_index"],
                                "text": chunk["text"],
                                "embedding": chunk_embedding,
                            })
                        surreal.upsert_wiki_chunks(embedded_chunks)
                        chunked += 1
                    except Exception as exc:
                        logger.warning("--rechunk: failed for scope=%s path=%s: %s", scope, path, exc)
                        errors += 1

                logger.info(
                    "--rechunk: scope=%s done chunked=%d errors=%d",
                    scope, chunked, errors,
                )
        except SurrealError as exc:
            logger.error("--rechunk: SurrealDB error for scope=%s: %s", scope, exc)


def main() -> None:
    """CLI entry point.

    Flags:
        --reingest   Reset all ingest cursors to seq=0 so the next ingestion
                     cycle re-reads the full corpus and re-derives entity roles
                     using the current type_map.  Exits immediately after reset.
        --recurate   Delete all non-human wiki_doc rows and drop
                     wiki_curator_cursor tables across every scope, then exit.
                     The next startup regenerates wiki pages cleanly.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Pinard memory ingester")
    parser.add_argument(
        "--reingest",
        action="store_true",
        help="Reset all ingest cursors to seq=0 and exit (triggers full re-type on next run)",
    )
    parser.add_argument(
        "--recurate",
        action="store_true",
        help=(
            "Delete non-human wiki_doc rows and drop wiki_curator_cursor across all scopes, "
            "then exit (next startup regenerates wiki pages cleanly)"
        ),
    )
    parser.add_argument(
        "--rechunk",
        action="store_true",
        help=(
            "Backfill wiki_chunk rows for all existing wiki_doc pages across all scopes "
            "then exit. Idempotent; use after a chunking strategy or embedding model change."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    _validate_env()

    if args.reingest:
        registry = OntologyRegistry()
        _reset_cursors_for_reingest(registry)
        return

    if args.recurate:
        registry = OntologyRegistry()
        _recurate_all_scopes(registry)
        return

    if args.rechunk:
        registry = OntologyRegistry()
        _rechunk_all_scopes(registry)
        return

    asyncio.run(run())


if __name__ == "__main__":
    main()
