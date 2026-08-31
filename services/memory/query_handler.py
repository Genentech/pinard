"""NATS request-reply handler for worker boot recipe queries.

Subscribes to `pinard.{vignoble}.query` and responds with a formatted
recipe summary drawn from SurrealDB (recall + lookup + trace).

Note: the subject is intentionally outside `pinard.*.memory.>` to avoid
being captured by the pinard-memory JetStream stream, which would cause
JetStream PubAcks to race ahead of the real reply on nc.request() calls.

The handler is fail-open: if SurrealDB is unavailable or the query fails, it
sends no reply — the worker falls back to git-YAML or proceeds without recipes.

Embedded subset (local load):
    If `MEMORY_EMBEDDED_SUBSET` is set, the handler opens the pointed-at
    embedded SurrealDB file (built by `export_subset`) and uses it in
    preference to the central SurrealDB server. This path is used when running
    an agent as a versioned ExoHub bundle (`harness + babysitter + memory`).
    The central server is used as a fallback when the embedded file is absent
    or fails to open.

Request payload::

    {"group_id": "genomics-build", "max_facts": 50}

Response payload::

    {"group_id": "genomics-build", "content": "[pipeline-knowledge]\\n..."}

Environment variables:
    MEMORY_EMBEDDED_SUBSET — Path to an embedded SurrealDB subset file
                              (built by export_subset). Takes priority over
                              the central server when set.
    SURREAL_URL   — SurrealDB endpoint (default: http://localhost:8000)
    SURREAL_USER  — SurrealDB root username
    SURREAL_PASS  — SurrealDB root password
    ROSETTA_URL   — Rosetta embedding endpoint (for semantic recall)
"""
from __future__ import annotations

import json
import logging
from typing import Any

import os

from .embeddings import EmbeddingError, embed
from .ontology.registry import OntologyRegistry
from .surrealdb.client import SurrealClient, SurrealError
from .surrealdb.embedded_client import EmbeddedClientError, load_embedded_subset

_EMBEDDED_SUBSET_PATH: str | None = os.environ.get("MEMORY_EMBEDDED_SUBSET")

logger = logging.getLogger("pinard.memory.query_handler")

_QUERY_TIMEOUT = 3.0  # seconds; fail-open on timeout


def _build_recipe_content(
    group_id: str,
    surreal: SurrealClient,
    query_text: str,
    max_facts: int,
) -> str:
    """Query SurrealDB and format operational knowledge as human-readable text.

    Sections:
      - Known Issues (LogPattern → Diagnosis → Action chains)
      - Pipeline Dependencies
      - Environment Prerequisites
      - Decision Points
    """
    lines: list[str] = ["[pipeline-knowledge]", f"group: {group_id}", ""]

    # ── Semantic recall ────────────────────────────────────────────────────────
    try:
        vec = embed(query_text)
        semantic_hits = surreal.recall(vec, limit=min(max_facts, 20))
    except (EmbeddingError, SurrealError) as exc:
        logger.debug("Semantic recall failed: %s", exc)
        semantic_hits = []

    # ── Full-text lookup for key operational terms ─────────────────────────────
    try:
        fts_hits = surreal.lookup(query_text, limit=min(max_facts, 10))
    except SurrealError as exc:
        logger.debug("Full-text lookup failed: %s", exc)
        fts_hits = []

    # Merge results, deduplicate by (role, name).
    seen: set[tuple[str, str]] = set()
    all_entities: list[dict[str, Any]] = []
    for hit in semantic_hits + fts_hits:
        key = (hit.get("role", ""), hit.get("name", ""))
        if key not in seen:
            seen.add(key)
            all_entities.append(hit)
    all_entities = all_entities[:max_facts]

    if not all_entities:
        return ""

    # ── Known Issues section ───────────────────────────────────────────────────
    log_patterns = [e for e in all_entities if e.get("role") == "log_pattern"]
    diagnoses = {e.get("name"): e for e in all_entities if e.get("role") == "diagnosis"}
    actions = {e.get("name"): e for e in all_entities if e.get("role") == "action"}

    if log_patterns:
        lines.append("## Known Issues")
        for lp in log_patterns:
            lines.append(f"- Pattern: {lp.get('name', '')}")
            desc = lp.get("description", "")
            if desc:
                lines.append(f"  Signal: {desc}")
            # Try to trace diagnosis and action via graph.
            try:
                neighbors = surreal.trace(
                    from_role="log_pattern",
                    from_name=lp.get("name", ""),
                    relation="indicates_problem",
                )
                for neighbor_row in neighbors:
                    for n in neighbor_row.get("neighbors", []):
                        diag_name = n.get("name", "")
                        if diag_name:
                            lines.append(f"  → Diagnosis: {diag_name}")
                            # Find resolution action.
                            try:
                                resolutions = surreal.trace(
                                    from_role="diagnosis",
                                    from_name=diag_name,
                                    relation="resolved_by",
                                )
                                for res_row in resolutions:
                                    for r in res_row.get("neighbors", []):
                                        lines.append(f"    → Action: {r.get('name', '')}")
                            except SurrealError:
                                pass
            except SurrealError:
                pass
        lines.append("")

    # ── Pipeline Dependencies ──────────────────────────────────────────────────
    tasks = [e for e in all_entities if e.get("role") in ("task", "step")]
    if tasks:
        lines.append("## Pipeline Steps")
        for t in tasks[:10]:
            name = t.get("name", "")
            desc = t.get("description", "")
            lines.append(f"- {name}" + (f": {desc[:120]}" if desc else ""))
        lines.append("")

    # ── Environment Prerequisites ──────────────────────────────────────────────
    conditions = [e for e in all_entities if e.get("role") == "environment_condition"]
    if conditions:
        lines.append("## Environment Prerequisites")
        for c in conditions:
            lines.append(f"- {c.get('name', '')}: {c.get('description', '')[:120]}")
        lines.append("")

    # ── Decision Points ────────────────────────────────────────────────────────
    decisions = [e for e in all_entities if e.get("role") == "decision"]
    if decisions:
        lines.append("## Decision Points")
        for d in decisions:
            desc = d.get("description", "")
            lines.append(f"- {d.get('name', '')}" + (f": {desc[:120]}" if desc else ""))
        lines.append("")

    # ── Rules & Facts ─────────────────────────────────────────────────────────
    rules = [e for e in all_entities if e.get("role") in ("artifact", "verdict") and e.get("data", {}).get("obs_type") in ("rule", "fact")]
    general = [e for e in all_entities if e.get("role") == "artifact" and e not in rules]
    if general:
        lines.append("## Operational Facts")
        for g in general[:10]:
            lines.append(f"- {g.get('name', '')}")
        lines.append("")

    return "\n".join(lines)


async def handle_query_message(
    msg: Any,
    registry: OntologyRegistry,
) -> None:
    """Handle a single `memory.query` request-reply message."""
    try:
        payload = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Malformed query payload: %s", exc)
        return  # No reply — fail-open.

    group_id = payload.get("group_id", "")
    max_facts = int(payload.get("max_facts", 50))
    query_text = payload.get("query", f"operational recipes for {group_id}")

    if not group_id:
        logger.debug("Query missing group_id; no reply")
        return

    if not msg.reply:
        logger.debug("Query has no reply subject; ignoring")
        return

    try:
        with _open_surreal_client(group_id) as surreal:
            content = _build_recipe_content(
                group_id=group_id,
                surreal=surreal,
                query_text=query_text,
                max_facts=max_facts,
            )
    except (SurrealError, Exception) as exc:
        logger.warning("SurrealDB unavailable for query group=%s: %s", group_id, exc)
        return  # Fail-open — no reply.

    if not content:
        logger.debug("No facts found for group=%s; no reply", group_id)
        return

    response = json.dumps({"group_id": group_id, "content": content}).encode()
    try:
        await msg._client.publish(msg.reply, response)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Failed to publish query reply: %s", exc)


def _open_surreal_client(group_id: str) -> Any:
    """Return a SurrealDB client, preferring the embedded subset when available.

    Returns either an `EmbeddedSurrealClient` (from `MEMORY_EMBEDDED_SUBSET`) or
    a `SurrealClient` connected to the central server. Both implement the same
    recall/lookup/trace interface used by `_build_recipe_content`.
    """
    if _EMBEDDED_SUBSET_PATH:
        try:
            client = load_embedded_subset(_EMBEDDED_SUBSET_PATH, group_id)
            logger.debug(
                "Using embedded subset %s for group=%s",
                _EMBEDDED_SUBSET_PATH, group_id,
            )
            return client
        except EmbeddedClientError as exc:
            logger.warning(
                "Embedded subset unavailable (%s); falling back to central SurrealDB", exc
            )
    return SurrealClient(group_id=group_id)
