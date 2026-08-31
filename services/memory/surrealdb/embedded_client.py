"""Embedded SurrealDB client for locally-loaded portable memory subsets.

Wraps the `surrealdb` Python package in embedded (`surrealkv://`) mode so that
an agent can load a portable subset file at boot without a running SurrealDB
server. Exposes the same query interface as `SurrealClient` — `recall_cosine_scan`,
`lookup`, `trace`, and `query_meta` — so `query_handler.py` can use either
transparently.

Typical usage::

    from services.memory.surrealdb.embedded_client import load_embedded_subset

    client = load_embedded_subset("/path/to/genomics-build.surrealkv", group_id="genomics-build")
    entities = client.recall_cosine_scan(embedding=[...])
    meta = client.query_meta()
    client.close()

    # Or as a context manager:
    with load_embedded_subset("/path/to/genomics-build.surrealkv", "genomics-build") as client:
        results = client.lookup("OOM shard 47")
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("pinard.memory.embedded_client")


def _normalize_rows(result: Any) -> list[dict[str, Any]]:
    """Normalize surrealdb SDK query() return value to a flat list of row dicts.

    The embedded SDK's high-level .query() returns a flat list of row dicts for
    the last statement — not the [[rows]] shape that SurrealClient.query() (via
    query_raw) produces.  This helper handles all three shapes that may appear:
      - flat list of row dicts  (embedded SDK .query())
      - nested list of rows     (result-sets wrapper)
      - legacy {status, result} wrapper  (old SDK versions)
    """
    if not result:
        return []
    if isinstance(result, list):
        first = result[0]
        if isinstance(first, dict) and "status" in first and "result" in first:
            return first.get("result", [])   # legacy {status, result} wrapper
        if isinstance(first, list):
            return first                      # nested result-sets
        return result                         # flat list of row dicts (SDK .query())
    return []


class EmbeddedClientError(RuntimeError):
    pass


class EmbeddedSurrealClient:
    """Read-only interface to an embedded SurrealDB subset file.

    All mutating operations are intentionally absent — the embedded file is a
    read-only snapshot from the central server.
    """

    NAMESPACE = "pinard"

    def __init__(self, db: Any, group_id: str) -> None:
        self._db = db
        self._group_id = group_id

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def __enter__(self) -> "EmbeddedSurrealClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Recall (full cosine similarity scan) ──────────────────────────────────

    def recall_cosine_scan(
        self,
        embedding: list[float],
        limit: int = 10,
        role_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find nearest entities by cosine similarity (full scan — no HNSW index).

        The embedded subset does not define an HNSW index (to avoid index-rebuild
        overhead at load time). Full cosine scan is adequate for subset sizes
        (typically ≤ a few hundred entities).
        """
        where = "WHERE embedding IS NOT NULL"
        if role_filter:
            where += " AND role = $role"
        sql = (
            f"SELECT *, vector::similarity::cosine(embedding, $vec) AS score "
            f"FROM entity {where} "
            f"ORDER BY score DESC LIMIT $limit"
        )
        vars: dict[str, Any] = {"vec": embedding, "limit": limit}
        if role_filter:
            vars["role"] = role_filter
        return self._query_first(sql, vars)

    # ── Lookup (full-text search) ─────────────────────────────────────────────

    def lookup(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        """Find entities by keyword match on name/description (BM25 FTS).

        Falls back to a simple LIKE scan if the FTS index is not available in
        the embedded file.
        """
        fts_sql = (
            "SELECT *, search::score(1) AS score FROM entity "
            "WHERE name @1@ $text OR description @1@ $text "
            "ORDER BY score DESC LIMIT $limit"
        )
        like_sql = (
            "SELECT * FROM entity "
            "WHERE string::contains(string::lowercase(name), string::lowercase($text)) "
            "OR string::contains(string::lowercase(description), string::lowercase($text)) "
            "LIMIT $limit"
        )
        vars: dict[str, Any] = {"text": text, "limit": limit}
        try:
            result = self._db.query(fts_sql, vars)
            if not result:
                return []
            return _normalize_rows(result)
        except Exception:
            # FTS index unavailable — degrade to case-insensitive LIKE scan.
            return self._query_first(like_sql, vars)

    # ── Trace (graph traversal) ───────────────────────────────────────────────

    def trace(
        self,
        from_role: str,
        from_name: str,
        relation: str,
        depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Traverse graph edges from an entity up to *depth* hops."""
        sql = (
            f"SELECT ->{relation}->entity.* AS neighbors "
            f"FROM entity WHERE role = $role AND name = $name LIMIT 1"
        )
        return self._query_first(sql, {"role": from_role, "name": from_name})

    # ── Metadata ──────────────────────────────────────────────────────────────

    def query_meta(self) -> dict[str, Any] | None:
        """Return the version-stamp metadata record written at export time."""
        results = self._query_first("SELECT * FROM subset_meta LIMIT 1", {})
        return results[0] if results else None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _query_first(self, sql: str, vars: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a query and return the first result set."""
        try:
            result = self._db.query(sql, vars)
        except Exception as exc:
            logger.debug("Embedded query error: %s | sql: %s", exc, sql[:120])
            return []
        if not result:
            return []
        return _normalize_rows(result)


# ── Factory ───────────────────────────────────────────────────────────────────

def load_embedded_subset(
    path: str | Path,
    group_id: str,
) -> EmbeddedSurrealClient:
    """Open an embedded SurrealDB subset file and return a client.

    Args:
        path: Path to the embedded SurrealDB file (written by `export_subset`).
        group_id: The SurrealDB database name (= vigne/pipeline scope).

    Returns:
        An `EmbeddedSurrealClient` ready for queries.

    Raises:
        EmbeddedClientError: If the file does not exist or cannot be opened.
    """
    from surrealdb import Surreal  # type: ignore[import]

    path = Path(path)
    if not path.exists():
        raise EmbeddedClientError(
            f"Embedded subset not found: {path}. "
            "Run `python -m services.memory.surrealdb.subset --group-id <id> --out <path>` "
            "to create it."
        )

    logger.info("Loading embedded subset from %s (group_id=%s)", path, group_id)
    try:
        db = Surreal(f"surrealkv://{path}")
        db.connect()
        db.use(EmbeddedSurrealClient.NAMESPACE, group_id)
    except Exception as exc:
        raise EmbeddedClientError(f"Failed to open embedded subset {path}: {exc}") from exc

    client = EmbeddedSurrealClient(db=db, group_id=group_id)

    # Log the version stamp for observability; do not fail if missing.
    try:
        meta = client.query_meta()
        if meta:
            logger.info(
                "Subset metadata: exported_at=%s core=%s domain=%s entities=%s edges=%s",
                meta.get("exported_at", "?"),
                meta.get("pinard_core_version", "?"),
                meta.get("domain_name") or "none",
                meta.get("entity_count", "?"),
                meta.get("edge_count", "?"),
            )
        else:
            logger.warning("No subset_meta record found in %s — subset may be malformed", path)
    except Exception as exc:
        logger.debug("Could not read subset_meta: %s", exc)

    return client
