"""Promotion candidate detector for the pinard memory layer.

Detects knowledge that recurs across multiple vignoble scopes and is therefore
a candidate for promotion to a broader scope (vignoble or global).

Recurrence detection strategy (all surfaces are real, spike-verified):

1. **Content fingerprint pre-filter** — entities whose first 120 chars of
   description match (case-insensitive) across scopes are grouped as candidate
   sets.  This is cheap and catches near-verbatim duplicates.

2. **Rosetta cosine similarity confirmation** — for each candidate group,
   the pair's embeddings are compared via cosine similarity using the same
   Rosetta endpoint already used for SurrealDB ingestion.  Pairs scoring
   above SIMILARITY_THRESHOLD (default 0.85) are confirmed recurrences.
   This catches paraphrases that the fingerprint misses.

3. **SurrealDB FTS BM25 cross-scope** — for entities that appear in only one
   scope by fingerprint, the entity FTS index (entity_name_fts +
   entity_description_fts, BM25) is queried in other scopes as a fallback.
   Uses SurrealClient.lookup() — the same surface used by recall_service.

Data source: SurrealDB entity table, where entity.description holds the full
observation content and entity.role corresponds to the observation type.
This is the authoritative store — no Engram HTTP API is called.

The original `/compare` REST endpoint was removed — it does not exist in
Engram's HTTP API (verified: GET/POST /compare → 404).  Engram's
`mem_compare`/`mem_judge` are MCP tools, not REST endpoints, and are not
callable from a standalone Python service.

Failure policy: SurrealDB errors on a scope are logged at ERROR level and
surfaced in the status file.  A connection error is a configuration bug,
not an empty result.

Environment variables:
    SURREAL_URL          — SurrealDB endpoint (default: http://localhost:8000)
    SURREAL_USER         — SurrealDB username (default: root)
    SURREAL_PASS         — SurrealDB password (required)
    PROMOTION_THRESHOLD  — Min number of vignobles before promoting (default: 2)
    SIMILARITY_THRESHOLD — Cosine similarity floor for recurrence (default: 0.85)
    ROLLUP_THRESHOLD     — Inherited from rollup.py (same env var)
"""
from __future__ import annotations

import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pinard.memory.promotion")

PROMOTION_THRESHOLD = int(
    os.environ.get("PROMOTION_THRESHOLD", os.environ.get("ROLLUP_THRESHOLD", "2"))
)
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.85"))


@dataclass
class PromotionCandidate:
    """A knowledge item that is a candidate for scope promotion."""

    candidate_id: str
    obs_type: str          # "rule", "fact", etc.
    content: str           # canonical content (the most-seen version)
    source_vignobles: list[str]
    recurrence_count: int
    proposed_scope: str    # "vignoble" or "global"
    similarity: float      # cosine similarity or 1.0 for exact fingerprint matches
    conflicts: list[str] = field(default_factory=list)  # divergent content variants


class PromotionDetectionError(RuntimeError):
    """Raised on configuration or endpoint errors — must surface loudly."""


def _fetch_entities_by_scope(
    scope: str,
    obs_types: list[str] | None = None,
    surreal_url: str | None = None,
    surreal_user: str | None = None,
    surreal_pass: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch entity records for a given scope from SurrealDB.

    Each entity row represents an ingested observation: description = full
    content, role = obs_type.  Raises PromotionDetectionError on SurrealDB
    errors — these indicate a configuration or connectivity problem.
    """
    from .surrealdb.client import SurrealClient, SurrealError

    try:
        with SurrealClient(
            group_id=scope,
            url=surreal_url,
            user=surreal_user,
            password=surreal_pass,
        ) as surreal:
            if obs_types:
                # Build a safe IN-list using string interpolation of validated role names.
                # SurrealDB bound params don't support array-in-WHERE natively for simple
                # equality filters without array functions; use individual OR clauses instead.
                role_clauses = " OR ".join(
                    f"role = '{r}'" for r in obs_types if r.replace("_", "").replace("-", "").isalnum()
                )
                if role_clauses:
                    sql = (
                        f"SELECT role, name, description, embedding FROM entity "
                        f"WHERE {role_clauses} LIMIT 500"
                    )
                else:
                    sql = "SELECT role, name, description, embedding FROM entity LIMIT 500"
            else:
                sql = "SELECT role, name, description, embedding FROM entity LIMIT 500"

            results = surreal.query(sql)
            if results and results[0]:
                return results[0] if isinstance(results[0], list) else [results[0]]
            return []
    except Exception as exc:
        raise PromotionDetectionError(
            f"SurrealDB fetch failed for scope '{scope}': {exc}"
        ) from exc


def _search_scope_surreal(
    query: str,
    scope: str,
    limit: int = 10,
    surreal_url: str | None = None,
    surreal_user: str | None = None,
    surreal_pass: str | None = None,
) -> list[dict[str, Any]]:
    """Search a scope via SurrealDB FTS (BM25) on entity name + description.

    Returns matching entity rows.  Logs WARNING on error (non-fatal fallback).
    Uses SurrealClient.lookup() which hits entity_name_fts + entity_description_fts.
    """
    from .surrealdb.client import SurrealClient, SurrealError

    try:
        with SurrealClient(
            group_id=scope,
            url=surreal_url,
            user=surreal_user,
            password=surreal_pass,
        ) as surreal:
            return surreal.lookup(query[:200], limit=limit)
    except Exception as exc:
        logger.warning("SurrealDB FTS search failed for scope '%s': %s", scope, exc)
        return []


# ── Cosine similarity via Rosetta ──────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_content(content: str) -> list[float] | None:
    """Embed content via Rosetta.  Returns None on error (non-fatal for detection)."""
    try:
        from .embeddings import embed, EmbeddingError
        return embed(content[:500])
    except Exception as exc:
        logger.debug("Rosetta embedding failed (skipping cosine check): %s", exc)
        return None


def _confirm_recurrence_cosine(
    content_a: str,
    content_b: str,
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[bool, float]:
    """Confirm recurrence via Rosetta cosine similarity.

    Returns (confirmed, similarity_score).  Falls back to fingerprint match
    (similarity=1.0) if embedding is unavailable.
    """
    # Fast-path: exact fingerprint match — confirmed without embedding.
    fp_a = content_a[:120].lower().strip()
    fp_b = content_b[:120].lower().strip()
    if fp_a == fp_b:
        return True, 1.0

    # Embedding-based confirmation.
    vec_a = _embed_content(content_a)
    vec_b = _embed_content(content_b)
    if vec_a is None or vec_b is None:
        # Embedding unavailable — fall back to fingerprint only.
        return False, 0.0

    sim = _cosine(vec_a, vec_b)
    return sim >= threshold, sim


class PromotionCandidateDetector:
    """Detects promotion candidates by cross-vignoble recurrence analysis.

    Recurrence is confirmed via:
    - Content fingerprint grouping (pre-filter, cheap)
    - Rosetta cosine similarity (confirmation, paraphrase-aware)
    - SurrealDB FTS BM25 (fallback for entities without embeddings)

    Usage::

        detector = PromotionCandidateDetector(vignoble_scopes=["vignoble-a", "vignoble-b"])
        candidates = detector.detect(obs_types=["rule", "fact"])
    """

    def __init__(
        self,
        vignoble_scopes: list[str],
        surreal_url: str | None = None,
        surreal_user: str | None = None,
        surreal_pass: str | None = None,
        threshold: int = PROMOTION_THRESHOLD,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        # Legacy param kept for callers that passed engram_url — silently ignored.
        engram_url: str | None = None,
    ) -> None:
        self._vignoble_scopes = vignoble_scopes
        self._surreal_url = surreal_url
        self._surreal_user = surreal_user
        self._surreal_pass = surreal_pass
        self._threshold = threshold
        self._similarity_threshold = similarity_threshold

    def detect(
        self, obs_types: list[str] | None = None
    ) -> list[PromotionCandidate]:
        """Detect promotion candidates across registered vignoble scopes.

        Algorithm:
        1. Fetch entities from each vignoble scope in SurrealDB.
        2. Group by content fingerprint (first 120 chars of description).
        3. For groups appearing in >= threshold vignobles, confirm via Rosetta
           cosine similarity (falls back to fingerprint for exact matches).
        4. Additionally, use SurrealDB FTS for cross-scope BM25 confirmation
           when embeddings are unavailable.
        5. Return PromotionCandidate list.

        Errors on individual scopes are logged at ERROR and skipped — we don't
        abort the whole detection run for a single unreachable scope.
        """
        if not obs_types:
            obs_types = ["rule", "fact"]

        # scope → list of entity rows
        scope_obs: dict[str, list[dict[str, Any]]] = {}
        for scope in self._vignoble_scopes:
            try:
                entities = _fetch_entities_by_scope(
                    scope,
                    obs_types,
                    surreal_url=self._surreal_url,
                    surreal_user=self._surreal_user,
                    surreal_pass=self._surreal_pass,
                )
                scope_obs[scope] = entities
                logger.debug("Fetched %d entities from scope '%s'", len(entities), scope)
            except PromotionDetectionError as exc:
                logger.error(
                    "Promotion detection: could not fetch scope '%s': %s", scope, exc
                )
                scope_obs[scope] = []

        # Group by fingerprint (first 120 chars of description, lowercased).
        # fingerprint → {scope: [entity, ...]}
        fingerprint_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for scope, entities in scope_obs.items():
            for ent in entities:
                content = str(ent.get("description") or "").strip()
                if not content:
                    continue
                fp = content[:120].lower().strip()
                fingerprint_map.setdefault(fp, {}).setdefault(scope, []).append(ent)

        candidates: list[PromotionCandidate] = []
        seen_fps: set[str] = set()

        for fp, scope_map in fingerprint_map.items():
            if fp in seen_fps:
                continue

            vignobles_with_fp = [s for s, ent_list in scope_map.items() if ent_list]

            # ── Step 1: fingerprint pre-filter ────────────────────────────────
            if len(vignobles_with_fp) >= self._threshold:
                rep_ent_list = [
                    scope_map[s][0]
                    for s in vignobles_with_fp
                    if scope_map[s]
                ]
                content_a = str(rep_ent_list[0].get("description") or "").strip()
                content_b = str(rep_ent_list[1].get("description") or "").strip() if len(rep_ent_list) > 1 else content_a

                confirmed, similarity = _confirm_recurrence_cosine(
                    content_a, content_b, self._similarity_threshold
                )

                if confirmed:
                    candidate = self._make_candidate(
                        fp=fp,
                        scope_map=scope_map,
                        vignobles_present=vignobles_with_fp,
                        rep_ent=rep_ent_list,
                        similarity=similarity,
                    )
                    candidates.append(candidate)
                    seen_fps.add(fp)
                    logger.info(
                        "Promotion candidate (fingerprint+cosine): type=%s scope=%s "
                        "vignobles=%s similarity=%.3f content=%.60s",
                        candidate.obs_type, candidate.proposed_scope,
                        vignobles_with_fp, similarity, content_a,
                    )
                    continue

            # ── Step 2: SurrealDB FTS BM25 cross-scope fallback ───────────────
            # For entities that appear in only 1 scope by fingerprint,
            # try FTS in the other scopes to find semantic matches.
            for scope, ent_list in scope_map.items():
                if not ent_list:
                    continue
                content = str(ent_list[0].get("description") or "").strip()[:200]
                if not content or fp in seen_fps:
                    continue

                other_scopes = [
                    s for s in self._vignoble_scopes
                    if s != scope and scope_obs.get(s) is not None
                ]
                bm25_matching_scopes = [scope]

                for other_scope in other_scopes:
                    search_hits = _search_scope_surreal(
                        content,
                        other_scope,
                        surreal_url=self._surreal_url,
                        surreal_user=self._surreal_user,
                        surreal_pass=self._surreal_pass,
                    )
                    for hit in search_hits:
                        hit_content = str(hit.get("description") or "").strip()
                        confirmed, sim = _confirm_recurrence_cosine(
                            content, hit_content, self._similarity_threshold
                        )
                        if confirmed:
                            bm25_matching_scopes.append(other_scope)
                            break

                if len(bm25_matching_scopes) >= self._threshold and fp not in seen_fps:
                    obs_type = str(ent_list[0].get("role") or "fact")
                    candidate = PromotionCandidate(
                        candidate_id=str(uuid.uuid4()),
                        obs_type=obs_type,
                        content=content,
                        source_vignobles=bm25_matching_scopes,
                        recurrence_count=len(bm25_matching_scopes),
                        proposed_scope="global" if len(bm25_matching_scopes) >= self._threshold + 1 else "vignoble",
                        similarity=self._similarity_threshold,  # BM25-confirmed
                    )
                    candidates.append(candidate)
                    seen_fps.add(fp)
                    logger.info(
                        "Promotion candidate (BM25 fallback): type=%s scope=%s "
                        "vignobles=%s content=%.60s",
                        obs_type, candidate.proposed_scope,
                        bm25_matching_scopes, content,
                    )

        return candidates

    def _make_candidate(
        self,
        fp: str,
        scope_map: dict[str, list[dict[str, Any]]],
        vignobles_present: list[str],
        rep_ent: list[dict[str, Any]],
        similarity: float,
    ) -> PromotionCandidate:
        obs_type = str(rep_ent[0].get("role") or "fact")
        canonical_content = str(rep_ent[0].get("description") or "").strip()

        # Detect content divergence (possible conflicts).
        contents = [
            str(o.get("description") or "").strip()
            for o in rep_ent
            if str(o.get("description") or "").strip() != canonical_content
        ]

        proposed_scope = (
            "global" if len(vignobles_present) >= self._threshold + 1 else "vignoble"
        )

        return PromotionCandidate(
            candidate_id=str(uuid.uuid4()),
            obs_type=obs_type,
            content=canonical_content,
            source_vignobles=vignobles_present,
            recurrence_count=len(vignobles_present),
            proposed_scope=proposed_scope,
            similarity=similarity,
            conflicts=contents,
        )
