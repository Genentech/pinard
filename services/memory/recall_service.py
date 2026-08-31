"""Mid-session recall service for the pinard memory layer.

Subscribes to `pinard.{vignoble}.recall` (core NATS request-reply, no
JetStream) and responds with summarized knowledge drawn from SurrealDB.

Note: the subject is intentionally outside `pinard.*.memory.>` to avoid
being captured by the pinard-memory JetStream stream, which would cause
JetStream PubAcks to race ahead of the real reply on nc.request() calls.

Exposes three typed intents (Decision A6):
  - recall  — semantic / vector neighbors (SurrealDB HNSW via Rosetta embed)
  - lookup  — lexical / full-text (SurrealDB FTS index)
  - trace   — KG / graph-chain traversal (SurrealDB RELATE)

Server-side intelligence:
  - Relevance gating: cosine distance threshold 0.65 (≈ cosine similarity ≥
    0.35, configurable via ``RECALL_DISTANCE_THRESHOLD`` env var). When all
    vector candidates exceed the threshold, the top-k are returned anyway so
    recall never yields empty while relevant candidates exist.
  - Summarization: Haiku compresses hits into a single paragraph ≤400 tokens.
    Degrades gracefully to verbatim top-hit text when LLM is unavailable.
  - Per-session dedup: in-memory dict {session_id → set(entity names already
    sent)}. State is held in RAM — lost on restart (at most one repeat).
  - Fail-open: any internal error → {"context": null}. Never surfaces to LLM.

Observability:
  - {VIGNOBLE_LOGS}/memory-service-status.json  — updated per query
  - {VIGNOBLE_LOGS}/memory-service.log          — one line per query

Request payload (NATS request body)::

    {
        "session_id": "genomics-step5-a1b2c3",
        "group_id": "genomics-build",
        "vignoble": "my-vignoble",
        "query": {
            "user_message": "The TileDB optimize step is failing...",
            "assistant_excerpt": "I see an OOM error...",
            "turn_index": 12
        },
        "constraints": {
            "max_context_tokens": 400,
            "exclude_session": "genomics-step5-a1b2c3"
        }
    }

Response payload::

    {
        "context": "[memory] ...",
        "sources": [...],
        "meta": {"total_candidates": 7, "returned_tokens": 85}
    }

Environment variables:
    NATS_VIGNOBLE           — Vignoble name (required)
    VIGNOBLE_LOGS           — Path to vignoble logs dir (default: ./logs)
    SURREAL_URL             — SurrealDB endpoint
    SURREAL_USER            — SurrealDB root username
    SURREAL_PASS            — SurrealDB root password
    ROSETTA_URL             — Rosetta embedding endpoint
    MEMORY_LLM_API          — Protocol adapter: ``openai-chat`` | ``anthropic-messages``
    MEMORY_LLM_BASE_URL     — Endpoint override
    MEMORY_LLM_MODEL        — Model id (overrides MEMORY_SUMMARIZE_MODEL)
    MEMORY_LLM_AUTH         — Token source: ``google-sa`` | ``url`` | ``static-key``
    MEMORY_TOKEN_URL        — Pour-token URL (used when MEMORY_LLM_AUTH=url or auto)
    ANTHROPIC_API_KEY       — Direct Anthropic key (MEMORY_LLM_AUTH=static-key)
    MEMORY_SUMMARIZE_MODEL  — Summarization model (legacy; use MEMORY_LLM_MODEL)
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .embeddings import EmbeddingError, embed
from .llm_client import LLMClient, LLMAuthError, LLMError
from .surrealdb.client import SCHEMA_PATH, SurrealClient, SurrealError
from .token_manager import LLMUnavailable, TokenManager

# ── Configuration ─────────────────────────────────────────────────────────────

VIGNOBLE = os.environ.get("NATS_VIGNOBLE", "default")
VIGNOBLE_LOGS = Path(os.environ.get("VIGNOBLE_LOGS", "./logs"))
# MEMORY_LLM_MODEL takes precedence; MEMORY_SUMMARIZE_MODEL is the legacy fallback.
_llm_model_env = os.environ.get("MEMORY_LLM_MODEL", "")
SUMMARIZE_MODEL = _llm_model_env or os.environ.get("MEMORY_SUMMARIZE_MODEL", "claude-haiku-4-5-20251001")
RECALL_SUBJECT = "pinard.*.recall"
# Global wiki database name — override via env to use a test/staging scope.
GLOBAL_WIKI_GROUP = os.environ.get("GLOBAL_WIKI_GROUP", "__global__")

# Relevance gating: maximum KNN distance to include a result.
# SurrealDB KNN returns cosine *distance* (0 = identical, 2 = opposite);
# we gate on distance ≤ 0.65 ≈ cosine similarity ≥ 0.35.
# Override via RECALL_DISTANCE_THRESHOLD env var (float).
RELEVANCE_DISTANCE_THRESHOLD = float(os.environ.get("RECALL_DISTANCE_THRESHOLD", "0.65"))
# Legacy safety-net knob — disabled by default (0). When >0, returns the
# top-N closest vector candidates even when all exceed the threshold.
RELEVANCE_MIN_TOP_K = int(os.environ.get("RECALL_MIN_TOP_K", "0"))
# Maximum token budget for summarized context.
DEFAULT_MAX_CONTEXT_TOKENS = 400
# Maximum characters per candidate sent to the summarizer (~4 chars/token).
_CHARS_PER_TOKEN = 4

# ── Logging setup ─────────────────────────────────────────────────────────────

VIGNOBLE_LOGS.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.RotatingFileHandler(
    VIGNOBLE_LOGS / "memory-service.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=3,
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

logger = logging.getLogger("pinard.memory.recall_service")
logger.setLevel(logging.INFO)
logger.addHandler(_file_handler)

# ── Status file ───────────────────────────────────────────────────────────────

_STATUS_FILE = VIGNOBLE_LOGS / "memory-service-status.json"


class _ServiceStatus:
    def __init__(self) -> None:
        self.state: str = "running"
        self.qdrant_available: bool = True   # used to track SurrealDB availability
        self.graphiti_available: bool = True  # reserved for future KG layer
        self.queries_today: int = 0
        self.queries_with_results: int = 0
        self.avg_query_latency_ms: float = 0.0
        self.dedup_sessions_tracked: int = 0
        self._day: str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        self._latency_sum_ms: float = 0.0
        self._latency_count: int = 0

    def _maybe_reset_day(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if today != self._day:
            self.queries_today = 0
            self.queries_with_results = 0
            self._latency_sum_ms = 0.0
            self._latency_count = 0
            self.avg_query_latency_ms = 0.0
            self._day = today

    def record_query(self, latency_ms: float, had_results: bool) -> None:
        self._maybe_reset_day()
        self.queries_today += 1
        if had_results:
            self.queries_with_results += 1
        self._latency_sum_ms += latency_ms
        self._latency_count += 1
        self.avg_query_latency_ms = self._latency_sum_ms / self._latency_count

    def write(self) -> None:
        data = {
            "state": self.state,
            "qdrant_available": self.qdrant_available,
            "graphiti_available": self.graphiti_available,
            "queries_today": self.queries_today,
            "queries_with_results": self.queries_with_results,
            "avg_query_latency_ms": round(self.avg_query_latency_ms, 1),
            "dedup_sessions_tracked": self.dedup_sessions_tracked,
        }
        tmp = _STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_STATUS_FILE)


# Module-level singletons — shared across all message handling in a process.
_status = _ServiceStatus()
_token_manager = TokenManager()
# Per-session dedup: {session_id: set of entity names already returned}
_session_dedup: dict[str, set[str]] = {}


# ── Intent handlers ───────────────────────────────────────────────────────────

def _handle_recall_intent(
    surreal: SurrealClient,
    query_text: str,
    limit: int = 20,
    embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Semantic vector recall via HNSW. Returns raw entity dicts."""
    vec = embedding
    if vec is None:
        try:
            vec = embed(query_text)
        except EmbeddingError as exc:
            logger.debug("Rosetta embed failed for recall intent: %s", exc)
            return []
    results = surreal.recall(vec, limit=limit)
    return results


def _handle_lookup_intent(
    surreal: SurrealClient,
    query_text: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Lexical full-text search via SurrealDB FTS index."""
    return surreal.lookup(query_text, limit=limit)


def _handle_trace_intent(
    surreal: SurrealClient,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Graph-chain traversal: expand log_pattern entities via 'indicates_problem'."""
    extra: list[dict[str, Any]] = []
    for entity in candidates:
        if entity.get("role") != "log_pattern":
            continue
        try:
            neighbors = surreal.trace(
                from_role="log_pattern",
                from_name=entity.get("name", ""),
                relation="indicates_problem",
            )
            for row in neighbors:
                for n in row.get("neighbors", []):
                    n["role"] = n.get("role", "diagnosis")
                    extra.append(n)
        except SurrealError:
            pass
    return extra


# ── Relevance gating ──────────────────────────────────────────────────────────

def _gate_by_relevance(
    entities: list[dict[str, Any]],
    threshold_distance: float = RELEVANCE_DISTANCE_THRESHOLD,
    min_top_k: int = RELEVANCE_MIN_TOP_K,
) -> list[dict[str, Any]]:
    """Return only entities with KNN distance ≤ threshold (cosine sim ≥ 0.35).

    Entities from FTS lookup and trace have no distance score — they pass
    through unconditionally (they are already keyword-matched or graph-adjacent).

    When no entity passes the gate (no FTS/trace hits and all vector candidates
    exceed the threshold), an empty list is returned. Callers treat empty as
    "no relevant results". The ``min_top_k`` parameter (default 0) is a legacy
    opt-in: when >0 it surfaces the closest N vector candidates even above the
    threshold, but this is disabled by default to avoid irrelevant hits.
    """
    gated = []
    vector_candidates: list[dict[str, Any]] = []
    for e in entities:
        dist = e.get("dist")
        if dist is None:
            # FTS/trace result — no vector distance; pass through.
            gated.append(e)
        elif dist <= threshold_distance:
            gated.append(e)
        else:
            vector_candidates.append(e)

    if not gated and vector_candidates and min_top_k > 0:
        vector_candidates.sort(key=lambda e: float(e.get("dist", 2.0)))
        gated.extend(vector_candidates[:min_top_k])

    return gated


# ── Per-session dedup ─────────────────────────────────────────────────────────

def _apply_dedup(
    session_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter out entities already returned to this session.

    Updates the dedup set for session_id. Keyed by (role, name).
    """
    global _session_dedup
    if session_id not in _session_dedup:
        _session_dedup[session_id] = set()
        _status.dedup_sessions_tracked = len(_session_dedup)

    already_sent = _session_dedup[session_id]
    fresh = []
    for e in candidates:
        if e.get("_wiki"):
            # Use a prefix that can't conflict with entity "role:name" keys.
            key = f"__wiki__:{e.get('path', '')}"
        else:
            key = f"{e.get('role','')}:{e.get('name','')}"
        if key not in already_sent:
            fresh.append(e)
            already_sent.add(key)
    return fresh


# ── Summarization ─────────────────────────────────────────────────────────────

def _summarize(
    entities: list[dict[str, Any]],
    max_tokens: int,
    group_id: str,
) -> str | None:
    """Use Haiku to compress entity hits into a ≤max_tokens paragraph.

    Falls back to verbatim top-hit text if LLM is unavailable.
    Returns None if entities is empty.
    """
    if not entities:
        return None

    # Build a compact text block of the entity hits.
    char_budget = max_tokens * _CHARS_PER_TOKEN
    lines: list[str] = []
    for e in entities:
        if e.get("_wiki"):
            title = e.get("title", "")
            body = e.get("body", "")
            line = f"[wiki] {title}"
            if body:
                line += f": {body[:200]}"
        else:
            name = e.get("name", "")
            desc = e.get("description", "")
            role = e.get("role", "")
            scope = e.get("_scope", "")
            provenance = e.get("provenance", "")
            if provenance == "mr":
                label = f"{role}:mr"
                if scope:
                    label += f" \u00b7 {scope}"
                line = f"[{label}] {name}"
            elif provenance == "mr-review":
                label = f"{role}:mr-review"
                if scope:
                    label += f" \u00b7 {scope}"
                line = f"[{label}] {name}"
            else:
                line = f"[{role}] {name}"
            if desc:
                line += f": {desc[:200]}"
        lines.append(line)
    raw_text = "\n".join(lines)[:char_budget]

    # Attempt LLM summarization.
    try:
        llm = _token_manager.get_client()
        prompt = (
            f"You are a technical memory assistant for an agent pipeline called {group_id}. "
            f"The following are relevant facts retrieved from memory:\n\n"
            f"{raw_text}\n\n"
            f"Write a single concise paragraph (≤{max_tokens} tokens) summarizing the most "
            f"actionable knowledge. Preserve: the specific fact, the resolution or action if known, "
            f"and when it was verified. Return only the paragraph, no preamble."
        )
        text = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        ).strip()
        if text:
            return f"[memory] {text}"
    except (LLMUnavailable, LLMAuthError, LLMError, Exception) as exc:
        logger.debug("Summarization LLM unavailable (%s); using verbatim fallback", exc)

    # Degraded mode: return verbatim top-hit text truncated to budget.
    top = entities[0]
    if top.get("_wiki"):
        name = top.get("title", "")
        desc = top.get("body", "")
    else:
        name = top.get("name", "")
        desc = top.get("description", "")
    text = f"{name}: {desc}" if desc else name
    return f"[memory] {text[:char_budget]}" if text else None


# ── Wiki intent handlers ─────────────────────────────────────────────────────

def _handle_wiki_intents(
    surreal: SurrealClient,
    query_text: str,
    embedding: list[float] | None,
    include_needs_review: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Run recall_wiki + lookup_wiki + recall_wiki_chunks and merge results, tagged as wiki hits.

    Chunk hits are collapsed to their parent page (best-scoring chunk per path).
    The matching chunk text is carried as _chunk for snippet display.
    """
    hits: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    if embedding is not None:
        try:
            for row in surreal.recall_wiki(embedding, limit=limit, include_needs_review=include_needs_review):
                path = row.get("path", "")
                if path not in seen_paths:
                    seen_paths.add(path)
                    row["_wiki"] = True
                    hits.append(row)
        except Exception as exc:
            logger.debug("recall_wiki failed: %s", exc)

    try:
        for row in surreal.lookup_wiki(query_text, limit=limit, include_needs_review=include_needs_review):
            path = row.get("path", "")
            if path not in seen_paths:
                seen_paths.add(path)
                row["_wiki"] = True
                hits.append(row)
    except Exception as exc:
        logger.debug("lookup_wiki failed: %s", exc)

    # Chunk recall pass: HNSW KNN over wiki_chunk, collapsed to parent page.
    if embedding is not None:
        try:
            chunk_rows = surreal.recall_wiki_chunks(embedding, limit=limit)
            # Collapse by parent_path: keep best (lowest dist) chunk per path.
            best_chunks: dict[str, dict[str, Any]] = {}
            for chunk in chunk_rows:
                parent_path = chunk.get("parent_path", "")
                if not parent_path:
                    continue
                dist = float(chunk.get("dist", 2.0))
                if parent_path not in best_chunks or dist < float(best_chunks[parent_path].get("dist", 2.0)):
                    best_chunks[parent_path] = chunk

            for parent_path, best_chunk in best_chunks.items():
                if parent_path in seen_paths:
                    continue
                # Fetch parent wiki_doc metadata (title, summary, type, status).
                parent_rows = surreal.query(
                    "SELECT path, title, type, summary, confidence, status FROM wiki_doc "
                    "WHERE path = $path LIMIT 1",
                    {"path": parent_path},
                )
                parent: dict[str, Any] | None = None
                if parent_rows and parent_rows[0]:
                    row0 = parent_rows[0]
                    parent = (row0[0] if isinstance(row0, list) else row0) or None

                if parent and not include_needs_review and parent.get("status") != "auto_serve":
                    continue

                seen_paths.add(parent_path)
                hit: dict[str, Any] = {
                    "path": parent_path,
                    "title": parent.get("title", parent_path) if parent else parent_path,
                    "type": parent.get("type", "") if parent else "",
                    "summary": parent.get("summary", "") if parent else "",
                    "confidence": parent.get("confidence", 1.0) if parent else 1.0,
                    "status": parent.get("status", "") if parent else "",
                    "dist": best_chunk.get("dist"),
                    "_wiki": True,
                    "_chunk": best_chunk.get("text", ""),
                }
                hits.append(hit)
        except Exception as exc:
            logger.debug("recall_wiki_chunks failed: %s", exc)

    return hits


# ── Exact fetch-by-ref ───────────────────────────────────────────────────────

def _fetch_across_scopes(
    ref: str,
    entitled_scopes: list[str],
) -> dict[str, Any] | None:
    """Resolve *ref* against SurrealDB, trying each scope in order.

    *ref* format:
      - ``wiki:<path>`` → exact wiki_doc lookup by path field.
      - ``entity:<record-id>`` → exact entity lookup by SurrealDB record id.
      - Bare string (no prefix) → tried as wiki path first, then entity id.

    Returns the first hit found, or None. Fail-open per scope.
    """
    if ref.startswith("wiki:"):
        wiki_path = ref[len("wiki:"):]
        entity_id: str | None = None
    elif ref.startswith("entity:"):
        wiki_path = ""
        # Keep the full "entity:<id>" string — SurrealDB record id format.
        entity_id = ref
    else:
        # Bare string: try as wiki path first, then as entity id.
        wiki_path = ref
        entity_id = ref

    for scope in entitled_scopes:
        try:
            with SurrealClient(group_id=scope) as surreal:
                surreal.ensure_schema()
                if wiki_path:
                    hit = surreal.fetch_wiki_by_path(wiki_path)
                    if hit:
                        hit["_wiki"] = True
                        hit["_scope"] = scope
                        return hit
                if entity_id:
                    hit = surreal.fetch_entity_by_id(entity_id)
                    if hit:
                        hit["_scope"] = scope
                        return hit
        except Exception as exc:
            logger.debug("fetch scope=%s ref=%r failed: %s", scope, ref, exc)
    return None


def _entitled_scopes(group_id: str, vignoble: str) -> list[str]:
    """Return the ordered list of SurrealDB group_ids the caller is entitled to.

    Order: vigne (group_id) → vignoble (vignoble-<name>) → global (__global__).
    """
    scopes: list[str] = []
    if group_id:
        scopes.append(group_id)
    if vignoble:
        scopes.append(f"vignoble-{vignoble}")
    scopes.append(GLOBAL_WIKI_GROUP)
    return scopes


# ── Sources builder ───────────────────────────────────────────────────────────

def _build_sources(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources = []
    for e in entities:
        if e.get("_wiki"):
            source: dict[str, Any] = {
                "type": "wiki",
                "path": e.get("path", ""),
                "title": e.get("title", ""),
                "confidence": e.get("confidence", 1.0),
                "status": e.get("status", ""),
            }
        else:
            source = {"type": "surrealdb", "role": e.get("role", "")}
            if e.get("id"):
                source["id"] = str(e["id"])
            # Expose MR-specific traceability fields for /recall fetch.
            if e.get("provenance") in ("mr", "mr-review"):
                data = e.get("data") or {}
                if data.get("url"):
                    source["url"] = data["url"]
                if data.get("files_changed"):
                    source["files_changed"] = data["files_changed"]
                if data.get("mr"):
                    source["mr"] = data["mr"]
        dist = e.get("dist")
        if dist is not None:
            source["score"] = round(1.0 - float(dist), 4)
        sources.append(source)
    return sources


# ── Multi-scope query helper ─────────────────────────────────────────────────

def _query_scope(
    scope: str,
    query_text: str,
    embedding: list[float] | None,
    include_needs_review: bool,
    seen_entity: set[tuple[str, str]],
    seen_wiki: set[str],
) -> list[dict[str, Any]]:
    """Query one SurrealDB scope database and return tagged hits.

    Runs recall + lookup + trace + wiki intents. Every returned hit carries
    a ``_scope`` field set to *scope*. Entity and wiki dedup keys are updated
    in-place in *seen_entity* / *seen_wiki* so the caller's global dedup stays
    consistent across scopes. Fail-open: any error returns an empty list.
    """
    hits: list[dict[str, Any]] = []
    try:
        with SurrealClient(group_id=scope) as surreal:
            surreal.ensure_schema()

            # Entity intents: recall (vector) + lookup (FTS).
            recall_hits = _handle_recall_intent(surreal, query_text, embedding=embedding)
            lookup_hits = _handle_lookup_intent(surreal, query_text)
            entity_candidates: list[dict[str, Any]] = []
            for hit in recall_hits + lookup_hits:
                key = (hit.get("role", ""), hit.get("name", ""))
                if key not in seen_entity:
                    seen_entity.add(key)
                    hit["_scope"] = scope
                    entity_candidates.append(hit)

            # Graph expansion (trace).
            trace_hits = _handle_trace_intent(surreal, entity_candidates)
            for hit in trace_hits:
                key = (hit.get("role", ""), hit.get("name", ""))
                if key not in seen_entity:
                    seen_entity.add(key)
                    hit["_scope"] = scope
                    entity_candidates.append(hit)

            hits.extend(entity_candidates)

            # Wiki intents.
            try:
                wiki_hits = _handle_wiki_intents(
                    surreal, query_text, embedding,
                    include_needs_review=include_needs_review,
                )
                for hit in wiki_hits:
                    path = hit.get("path", "")
                    if path not in seen_wiki:
                        seen_wiki.add(path)
                        hit["_scope"] = scope
                        hits.append(hit)
            except Exception as exc:
                logger.debug("Wiki intents failed for scope=%s: %s", scope, exc)

    except Exception as exc:
        logger.debug("Query scope=%s failed: %s", scope, exc)

    return hits


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_recall_message(msg: Any) -> None:
    """Handle a single `memory.recall` request-reply message.

    Fail-open: any error → {"context": null}, no exception propagates.
    """
    t0 = time.monotonic()

    # Parse payload.
    try:
        payload = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("Malformed recall payload: %s", exc)
        await _reply_null(msg)
        return

    if not msg.reply:
        logger.debug("Recall request has no reply subject; ignoring")
        return

    session_id = payload.get("session_id", "")
    group_id = payload.get("group_id", "")
    query = payload.get("query", {})
    constraints = payload.get("constraints", {})
    raw_mode = bool(payload.get("raw", False))
    type_filter = str(payload.get("type_filter", "")).strip().lower()

    user_message = str(query.get("user_message", ""))[:500]
    assistant_excerpt = str(query.get("assistant_excerpt", ""))[:500]
    turn_index = int(query.get("turn_index", 0))
    max_context_tokens = int(constraints.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
    include_needs_review = bool(constraints.get("include_needs_review", False))

    # ── Fetch-by-ref path (exact drill-down, no query pipeline) ──────────
    fetch_ref = str(payload.get("fetch", "")).strip()
    vignoble = str(payload.get("vignoble", "")).strip()
    if fetch_ref:
        t_fetch = time.monotonic()
        result: dict[str, Any] | None = None
        try:
            scopes = _entitled_scopes(group_id, vignoble)
            hit = _fetch_across_scopes(fetch_ref, scopes)
            if hit:
                if hit.get("_wiki"):
                    result = {
                        "type": "wiki",
                        "ref": fetch_ref,
                        "path": hit.get("path", ""),
                        "title": hit.get("title", ""),
                        "body": hit.get("body", ""),
                        "confidence": hit.get("confidence"),
                        "status": hit.get("status", ""),
                        "scope": hit.get("_scope", ""),
                    }
                else:
                    result = {
                        "type": "entity",
                        "ref": fetch_ref,
                        "id": str(hit.get("id", "")),
                        "role": hit.get("role", ""),
                        "name": hit.get("name", ""),
                        "description": hit.get("description", ""),
                        "scope": hit.get("_scope", ""),
                    }
        except Exception as exc:
            logger.warning("fetch ref=%r error: %s", fetch_ref, exc)
        latency_ms = (time.monotonic() - t_fetch) * 1000
        found = result is not None
        logger.info(
            "fetch session=%s group=%s ref=%r found=%s latency_ms=%.0f",
            session_id, group_id, fetch_ref, found, latency_ms,
        )
        fetch_response = {
            "result": result,
            "meta": {"ref": fetch_ref, "found": found},
        }
        try:
            await msg._client.publish(msg.reply, json.dumps(fetch_response, default=str).encode())  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("Failed to publish fetch reply: %s", exc)
        return

    if not group_id:
        await _reply_null(msg)
        return

    query_text = f"{user_message} {assistant_excerpt}".strip() or f"operational knowledge for {group_id}"

    # Determine which scopes to query.
    # Extension passes `scopes` (full fan-out list) plus optional `scope` override.
    # Fall back to _entitled_scopes for callers that don't send `scopes`.
    scope_override = str(payload.get("scope", "")).strip()
    payload_scopes: list[str] = payload.get("scopes", [])
    if isinstance(payload_scopes, list) and payload_scopes:
        query_scopes = payload_scopes
    else:
        query_scopes = _entitled_scopes(group_id, vignoble)
    # --scope / --global flag narrows to a single scope.
    if scope_override:
        query_scopes = [scope_override]

    # Run recall pipeline.
    context: str | None = None
    sources: list[dict[str, Any]] = []
    total_candidates = 0

    # Pre-compute embedding once for all scopes.
    recall_embedding: list[float] | None = None
    try:
        recall_embedding = embed(query_text)
    except Exception as exc:
        logger.debug("Embedding failed, will skip vector recall paths: %s", exc)

    merged: list[dict[str, Any]] = []

    try:
        # Fan out across all scopes; dedup entity keys globally across scopes.
        seen_entity: set[tuple[str, str]] = set()
        seen_wiki: set[str] = set()

        for scope in query_scopes:
            scope_hits = _query_scope(
                scope, query_text, recall_embedding,
                include_needs_review, seen_entity, seen_wiki,
            )
            merged.extend(scope_hits)

        total_candidates = len(merged)

        # Apply type_filter if requested.
        if type_filter and type_filter != "engram":
            if type_filter == "lesson":
                merged = [h for h in merged if h.get("provenance") == "lesson"]
            elif type_filter == "teaching":
                merged = [h for h in merged if h.get("provenance") == "episode_extraction"]
            elif type_filter == "wiki":
                merged = [h for h in merged if h.get("_wiki")]
            else:
                # Treat as entity role match.
                merged = [h for h in merged if not h.get("_wiki") and h.get("role", "") == type_filter]

        # Global relevance sort: vector hits ascending by dist; no-dist hits after.
        if raw_mode:
            vector_hits = sorted(
                [h for h in merged if h.get("dist") is not None],
                key=lambda h: float(h["dist"]),
            )
            nodist_hits = [h for h in merged if h.get("dist") is None]
            merged = vector_hits + nodist_hits

        if raw_mode:
            # Raw mode: return verbatim hits for tool use — no gating, dedup, or summarization.
            _status.qdrant_available = True
        else:
            # 5. Relevance gating.
            gated = _gate_by_relevance(merged)

            # 6. Per-session dedup (wiki uses "wiki:<path>" key; entities use "role:name").
            if session_id:
                fresh = _apply_dedup(session_id, gated)
            else:
                fresh = gated

            # 7. Summarize.
            if fresh:
                context = _summarize(fresh, max_context_tokens, group_id)
                sources = _build_sources(fresh)

            _status.qdrant_available = True

    except Exception as exc:
        logger.warning("Recall pipeline error group=%s scopes=%s: %s", group_id, query_scopes, exc)
        await _reply_null(msg)
        return

    latency_ms = (time.monotonic() - t0) * 1000
    _status.record_query(latency_ms, had_results=bool(merged if raw_mode else context))
    _status.write()

    if raw_mode:
        returned_tokens = 0
        logger.info(
            "session=%s group=%s scopes=%s turn=%d candidates=%d raw=true latency_ms=%.0f",
            session_id, group_id, query_scopes, turn_index, total_candidates, latency_ms,
        )
        response = {
            "hits": merged,
            "meta": {"total_candidates": total_candidates},
        }
    else:
        returned_tokens = len(context) // _CHARS_PER_TOKEN if context else 0
        logger.info(
            "session=%s group=%s scopes=%s turn=%d candidates=%d returned_tokens=%d latency_ms=%.0f was_empty=%s",
            session_id, group_id, query_scopes, turn_index, total_candidates, returned_tokens,
            latency_ms, not context,
        )
        response = {
            "context": context,
            "sources": sources,
            "meta": {
                "total_candidates": total_candidates,
                "returned_tokens": returned_tokens,
            },
        }
    try:
        await msg._client.publish(msg.reply, json.dumps(response, default=str).encode())  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Failed to publish recall reply: %s", exc)


async def _reply_null(msg: Any) -> None:
    """Send a null-context response (fail-open)."""
    if not msg.reply:
        return
    response = {"context": None, "sources": [], "meta": {"total_candidates": 0, "returned_tokens": 0}}
    try:
        await msg._client.publish(msg.reply, json.dumps(response, default=str).encode())  # type: ignore[attr-defined]
    except Exception:
        pass


# ── Subscription factory ──────────────────────────────────────────────────────

async def start_recall_subscription(nc: Any) -> None:
    """Subscribe to memory.recall on the given NATS connection.

    Called once from the ingester's run() coroutine. The subscription is
    non-blocking — messages are handled via the async callback.
    """
    async def _cb(msg: Any) -> None:
        await handle_recall_message(msg)

    await nc.subscribe(RECALL_SUBJECT, cb=_cb)
    logger.info("Recall service listening on %s (all vignobles)", RECALL_SUBJECT)
    _status.state = "running"
    _status.write()


# ── Boot-recall: hierarchical, scope-labeled, compact manifest ───────────────
# Serves the babysitter's spawn-time knowledge injection (#155, #163): fans out
# across the global → vignoble → vigne scope hierarchy and returns a compact
# manifest of index entries (type · title · summary · ref) — no bodies.
#
# Content selection by tier:
#   - above vigne (global, vignoble-*): wiki-only entries.
#   - vigne (agent's own group_id): wiki + top typed-entity entries.
#   - raw `artifact` entities: dropped at all tiers.

BOOT_RECALL_SUBJECT = "pinard.*.recall.boot"
# Maximum entries returned per scope.
_BOOT_TOP_K_DEFAULT = 5
# Typed entity roles surfaced at the vigne tier (excludes raw `artifact`).
_TYPED_ENTITY_ROLES: frozenset[str] = frozenset({
    "decision", "diagnosis", "gotcha", "action", "log_pattern",
    "concept", "requirement", "hypothesis",
})


_HEADING_RE = re.compile(r"^#+\s*")


def _first_sentence(text: str) -> str:
    """Return the first sentence of *text*, or the whole text if no sentence break.

    Skips leading markdown headings and blank lines before extracting the first
    real prose sentence. Finds the earliest sentence-ending punctuation (., !, ?)
    or newline.
    """
    text = text.strip()
    if not text:
        return text
    # Skip leading blank lines and markdown headings to reach prose.
    prose = text
    for line in text.splitlines():
        if line and not _HEADING_RE.match(line):
            prose = line
            break
    else:
        # All lines were blank or headings — fall back to full stripped text.
        prose = text
    # Find the earliest terminator across all separator types.
    best: int | None = None
    for sep in (".", "!", "?", "\n"):
        idx = prose.find(sep)
        if idx != -1 and (best is None or idx < best):
            best = idx
    if best is not None and best < 200:
        return prose[: best + 1].strip()
    return prose[:200].strip()


_PROVENANCE_RE = re.compile(r"\s*\([^)]*\)\s*$")
_HEADING_MARK_RE = re.compile(r"^#+\s*")
# Matches mem_save template section labels: "What:", "**What**:", "Why:", etc.
_SECTION_LABEL_RE = re.compile(
    r"^(?:\*{1,2})?(?:What|Why|Where|Learned|How|Note|Result|Context)(?:\*{1,2})?\s*:\.?\s*",
    re.IGNORECASE,
)
# Matches surrounding **bold** or *italic* markers on the whole string.
_BOLD_WRAP_RE = re.compile(r"^\*{1,2}(.+?)\*{1,2}$")
# Matches a **bold** or *italic* leading prefix (may have trailing : or whitespace).
_BOLD_PREFIX_RE = re.compile(r"^\*{1,2}([^*]+?)\*{1,2}\s*:?\s*")


def _strip_entity_noise(text: str) -> str:
    """Strip mem_save section labels and bold/italic wrappers from *text*.

    Used to normalise both title and summary before deduplication comparison,
    and to clean the summary before display so template labels never appear.
    """
    text = text.strip()
    # Strip section labels (What:, **Why**:, etc.) — loop until stable.
    prev = None
    while prev != text:
        prev = text
        text = _SECTION_LABEL_RE.sub("", text).strip()
    # Strip surrounding **bold** / *italic* wrappers (full wrap).
    m = _BOLD_WRAP_RE.match(text)
    if m:
        text = m.group(1).strip()
    else:
        # Strip leading **bold prefix**: e.g. "**Progress (2026-07-14)**:" → "Progress (2026-07-14):"
        m = _BOLD_PREFIX_RE.match(text)
        if m and m.end() == len(text):
            # Only apply when the bold prefix IS the entire string (nothing follows).
            text = m.group(1).strip()
    return text


def _clean_entity_title(name: str, max_len: int = 120) -> str:
    """Deterministically clean a typed-entity name for use as a boot-manifest title.

    - Strips leading markdown heading marks (``#``)
    - Strips leading mem_save template labels like ``What:``, ``**Why**:``, etc.
    - Strips surrounding ``**bold**`` wrappers
    - Strips trailing ``:`` and whitespace
    - Strips trailing provenance parentheticals like ``(clarified by ...)``
    - Caps length to *max_len* characters

    Iterates until stable so that any interleaved order of trailing junk is
    handled correctly (e.g. ``(clarified by lelongs):`` — colon after paren).
    """
    title = name.strip()
    prev = None
    while prev != title:
        prev = title
        title = _HEADING_MARK_RE.sub("", title).strip()
        title = _SECTION_LABEL_RE.sub("", title).strip()
        m = _BOLD_WRAP_RE.match(title)
        if m:
            title = m.group(1).strip()
        title = title.rstrip(":").strip()
        title = _PROVENANCE_RE.sub("", title).strip()
    if max_len > 0 and len(title) > max_len:
        title = title[:max_len].strip()
    return title or name.strip()


def _boot_hits_for_scope(
    scope: str,
    vigne_scope: str,
    query_text: str,
    embedding: list[float] | None,
    top_k: int,
) -> list[dict[str, Any]]:
    """Query one scope database and return compact manifest entries.

    Each entry: {scope, type, title, summary, ref} — no body/snippet.

    Content selection:
    - vigne_scope (the agent's own group_id): wiki + typed entities (not artifact).
    - any other scope (vignoble-*, __global__): wiki only.

    Fail-open: any error for this scope returns an empty list.
    """
    is_vigne = (scope == vigne_scope)

    try:
        with SurrealClient(group_id=scope) as surreal:
            surreal.ensure_schema()

            # Wiki hits — always included.
            wiki_hits: list[dict[str, Any]] = []
            try:
                wiki_hits = _handle_wiki_intents(
                    surreal, query_text, embedding,
                    include_needs_review=False, limit=top_k,
                )
            except Exception:
                pass

            # Typed entity hits — vigne tier only.
            merged_entities: list[dict[str, Any]] = []
            if is_vigne:
                entity_hits: list[dict[str, Any]] = []
                if embedding is not None:
                    try:
                        entity_hits = surreal.recall(embedding, limit=top_k)
                    except Exception:
                        pass

                fts_hits: list[dict[str, Any]] = []
                try:
                    fts_hits = surreal.lookup(query_text, limit=top_k)
                except Exception:
                    pass

                seen: set[tuple[str, str]] = set()
                for hit in entity_hits + fts_hits:
                    role = hit.get("role", "")
                    if role not in _TYPED_ENTITY_ROLES:
                        continue
                    key = (role, hit.get("name", ""))
                    if key not in seen:
                        seen.add(key)
                        merged_entities.append(hit)

    except Exception as exc:
        logger.debug("Boot recall scope=%s failed: %s", scope, exc)
        return []

    # Shape into compact manifest entries: {scope, type, title, summary, ref}.
    result: list[dict[str, Any]] = []

    for h in wiki_hits[:top_k]:
        title = h.get("title", "")
        if not title:
            continue
        # Prefer the intentional LLM-synthesized summary stored in wiki_doc;
        # fall back to first_sentence(body) for older pages that lack it.
        stored_summary = h.get("summary", "") or ""
        body = h.get("body", "") or ""
        summary = stored_summary if stored_summary else (_first_sentence(body) if body else title)
        path = h.get("path", "")
        result.append({
            "scope": scope,
            "type": "wiki",
            "title": title,
            "summary": summary,
            "ref": f"wiki:{path}",
        })

    for h in merged_entities[:top_k]:
        raw_name = h.get("name", "")
        if not raw_name:
            continue
        title = _clean_entity_title(raw_name)
        desc = h.get("description", "") or ""
        summary = _first_sentence(desc) if desc else title
        # Clean and dedupe: normalise both sides (strip section labels + bold wrappers)
        # before comparing so "**What**: A future MR…" == "A future MR…" is caught.
        # Also use the cleaned summary for display so labels never appear.
        summary = _strip_entity_noise(summary)
        _t = title[:60].lower()
        _s = summary[:60].lower()
        if not summary or _s == _t or _s.startswith(_t) or _t.startswith(_s):
            summary = ""
        # SurrealDB RecordID already stringifies as "entity:<hash>" — avoid double-prefix.
        entity_id = str(h.get("id", ""))
        ref = entity_id if entity_id.startswith("entity:") else f"entity:{entity_id}"
        result.append({
            "scope": scope,
            "type": h.get("role", "entity"),
            "title": title,
            "summary": summary,
            "ref": ref,
        })

    return result


async def handle_boot_message(msg: Any) -> None:
    """Handle a boot-recall request.

    Request: {
        scopes: list[str],
        group_id: str,           # the agent's own vigne scope
        vignoble?: str,          # vignoble name (informational)
        task_text?: str,
        top_k?: int,
    }
    Response: {
        entries: [{scope, type, title, summary, ref}, ...],
        meta: {total_entries: int}
    }

    Fail-open: any internal error → {entries: [], meta: {total_entries: 0}}.
    """
    t0 = time.monotonic()

    if not msg.reply:
        logger.debug("Boot recall request has no reply subject; ignoring")
        return

    try:
        payload = json.loads(msg.data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("Malformed boot recall payload: %s", exc)
        await _reply_boot_empty(msg)
        return

    scopes: list[str] = payload.get("scopes", [])
    group_id: str = str(payload.get("group_id", "")).strip()
    task_text: str = str(payload.get("task_text", ""))[:500]
    top_k: int = int(payload.get("top_k", _BOOT_TOP_K_DEFAULT))
    # snippet_chars accepted for backward compat but ignored (no snippets in v2).

    if not scopes:
        await _reply_boot_empty(msg)
        return

    # The vigne scope is the agent's own group_id (innermost tier).
    vigne_scope = group_id

    query_text = task_text or "accumulated operational knowledge"

    # Pre-compute embedding once for all scopes.
    embedding: list[float] | None = None
    try:
        embedding = embed(query_text)
    except Exception as exc:
        logger.debug("Boot recall embedding failed, falling back to FTS-only: %s", exc)

    # Fan out across scopes (synchronous per scope, best-effort).
    all_entries: list[dict[str, Any]] = []
    for scope in scopes:
        entries = await asyncio.get_event_loop().run_in_executor(
            None,
            _boot_hits_for_scope,
            scope, vigne_scope, query_text, embedding, top_k,
        )
        all_entries.extend(entries)

    latency_ms = (time.monotonic() - t0) * 1000
    logger.info(
        "boot scopes=%s group_id=%r task_text=%r total_entries=%d latency_ms=%.0f",
        scopes, group_id, task_text[:50], len(all_entries), latency_ms,
    )

    response = {"entries": all_entries, "meta": {"total_entries": len(all_entries)}}
    try:
        await msg._client.publish(msg.reply, json.dumps(response, default=str).encode())  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Failed to publish boot recall reply: %s", exc)


async def _reply_boot_empty(msg: Any) -> None:
    """Send an empty boot-recall response (fail-open)."""
    if not msg.reply:
        return
    try:
        await msg._client.publish(msg.reply, json.dumps({"entries": [], "meta": {"total_entries": 0}}).encode())  # type: ignore[attr-defined]
    except Exception:
        pass


async def start_boot_recall_subscription(nc: Any) -> None:
    """Subscribe to the boot-recall subject on the given NATS connection."""
    async def _cb(msg: Any) -> None:
        await handle_boot_message(msg)

    await nc.subscribe(BOOT_RECALL_SUBJECT, cb=_cb)
    logger.info("Boot recall service listening on %s (all vignobles)", BOOT_RECALL_SUBJECT)
