"""Engram cloud RDS reader for the pinard memory layer (EKS path).

Reads curated observations directly from the Engram cloud Postgres database via
the ``cloud_mutations`` table (read-only role ``engram_ro``).  Used when the
local Engram HTTP read API is not available (i.e. in EKS where ``engram serve``
does not run).

Why ``cloud_mutations`` instead of ``observations``?
The cloud store keeps one row per synced entity in ``cloud_mutations`` with a
monotonic ``seq`` cursor — there is no ``observations`` table in the cloud DB.
The ``payload`` JSONB column carries the full observation record.

Schema (live, validated against the production RDS instance):
    cloud_mutations:
        seq         bigint PK  — monotonic cursor; use ``seq > $last`` for incremental reads
        project     text       — == group_id / Engram project name
        entity      text       — 'observation' | 'prompt' | 'session'
        entity_key  text
        op          text       — 'upsert' | 'delete'
        payload     jsonb      — {id, type, scope, title, content, project,
                                  sync_id, created_at, session_id, ...}
        occurred_at timestamptz

Cursor persistence:
    Per-project ``seq`` cursors are stored in a JSON file at
    ``MEMORY_CURSOR_FILE`` (default ``{VIGNOBLE_LOGS}/engram-pg-cursors.json``).
    On each poll the reader loads the last ``seq`` for the project, queries
    ``seq > $last``, and persists the new max ``seq`` after a successful read.
    This gives exact incremental delivery with no timestamp fuzziness.

Environment variables:
    ENGRAM_PG_DSN         — Postgres DSN (postgresql://user:pass@host/db?sslmode=require)
    MEMORY_CURSOR_FILE    — Path to the seq-cursor JSON (default: logs/engram-pg-cursors.json)
    VIGNOBLE_LOGS         — Overrides the default log-dir used for the cursor file
    MEMORY_PG_BATCH       — Max rows per poll (default: 1000)

Usage::

    reader = EngramPostgresReader(project="exo-cli")
    observations = reader.fetch()
    for obs in observations:
        print(obs.obs_id, obs.obs_type, obs.content)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

ENGRAM_PG_DSN: str = os.environ.get("ENGRAM_PG_DSN", "")
MEMORY_PG_BATCH: int = int(os.environ.get("MEMORY_PG_BATCH", "1000"))

_VIGNOBLE_LOGS = Path(os.environ.get("VIGNOBLE_LOGS", "./logs"))
_DEFAULT_CURSOR_FILE = Path(
    os.environ.get("MEMORY_CURSOR_FILE", str(_VIGNOBLE_LOGS / "engram-pg-cursors.json"))
)


class EngramPostgresReaderError(RuntimeError):
    pass


@runtime_checkable
class CursorStore(Protocol):
    """Protocol for seq-cursor backends (file or SurrealDB)."""

    def get(self, project: str) -> int: ...
    def update(self, project: str, seq: int) -> None: ...


@dataclass
class _Cursor:
    """Per-project seq-cursor backed by a JSON file."""

    path: Path
    _data: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read cursor file %s: %s — starting from 0", self.path, exc)

    def get(self, project: str) -> int:
        return self._data.get(project, 0)

    def update(self, project: str, seq: int) -> None:
        self._data[project] = seq
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)


# Module-level shared cursor so successive calls within one process share state.
_cursor: _Cursor | None = None


def _get_cursor(path: Path = _DEFAULT_CURSOR_FILE) -> _Cursor:
    global _cursor
    if _cursor is None or _cursor.path != path:
        _cursor = _Cursor(path=path)
    return _cursor


# Re-export EngramObservation from engram_reader so callers can use one type.
from .engram_reader import EngramObservation  # noqa: E402


class EngramPostgresReader:
    """Reads curated Engram observations from the cloud Postgres RDS.

    Each instance is scoped to a single ``project`` (== Engram group_id).
    Incremental delivery is guaranteed by the per-project ``seq`` cursor.
    ``op='delete'`` rows are currently skipped (tombstone support is a
    follow-up when delete propagation is needed downstream).
    """

    _SQL = """
        SELECT seq, entity_key, op, payload
        FROM cloud_mutations
        WHERE entity = 'observation'
          AND project = %s
          AND seq > %s
        ORDER BY seq
        LIMIT %s
    """

    def __init__(
        self,
        project: str,
        dsn: str = ENGRAM_PG_DSN,
        batch: int = MEMORY_PG_BATCH,
        cursor_path: Path = _DEFAULT_CURSOR_FILE,
        cursor_store: CursorStore | None = None,
        advance_cursor: bool = True,
    ) -> None:
        if not dsn:
            raise EngramPostgresReaderError(
                "ENGRAM_PG_DSN is not set — cannot connect to Engram Postgres"
            )
        self.project = project
        self._dsn = dsn
        self._batch = batch
        self._cursor_path = cursor_path
        self._cursor_store = cursor_store
        self._advance_cursor = advance_cursor
        self.max_seq: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self) -> list[EngramObservation]:
        """Fetch new observations since the last cursor position.

        Raises EngramPostgresReaderError on connection or query failure.
        Updates the per-project cursor only after a successful read.
        """
        return list(self._fetch_incremental())

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_incremental(self) -> Iterator[EngramObservation]:
        try:
            import psycopg
        except ImportError:
            raise EngramPostgresReaderError(
                "psycopg is not installed — add psycopg[binary] to requirements.txt"
            )

        cursor_store: CursorStore = self._cursor_store if self._cursor_store is not None else _get_cursor(self._cursor_path)
        last_seq = cursor_store.get(self.project)

        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(self._SQL, (self.project, last_seq, self._batch))
                    rows = cur.fetchall()
        except Exception as exc:
            raise EngramPostgresReaderError(
                f"Engram Postgres query failed for project '{self.project}': {exc}"
            ) from exc

        if not rows:
            logger.debug(
                "No new observations for project '%s' (last_seq=%d)", self.project, last_seq
            )
            return

        max_seq = last_seq
        count = 0
        for row in rows:
            seq: int = row[0]
            op: str = row[2]
            payload: dict = row[3]

            if seq > max_seq:
                max_seq = seq

            if op == "delete":
                # Tombstone — skip for now; downstream SurrealDB cleanup is a follow-up.
                logger.debug(
                    "Skipping delete tombstone for project '%s' seq=%d entity_key=%s",
                    self.project, seq, row[1],
                )
                continue

            obs = self._parse_payload(payload)
            if obs is not None:
                count += 1
                yield obs

        # Always expose the computed max_seq so callers can advance the cursor themselves.
        self.max_seq = max_seq

        if self._advance_cursor:
            # Default behaviour (http/file-cursor path): advance immediately after fetch.
            cursor_store.update(self.project, max_seq)

        logger.info(
            "Fetched %d observations for project '%s' (seq %d → %d)",
            count, self.project, last_seq, max_seq,
        )

    def _parse_payload(self, payload: dict) -> EngramObservation | None:
        """Parse a cloud_mutations JSONB payload into an EngramObservation."""
        try:
            raw_ts = payload.get("created_at") or payload.get("occurred_at") or ""
            if raw_ts:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            else:
                ts = datetime.now(tz=timezone.utc)

            content = str(payload.get("content") or payload.get("body") or "")
            if not content:
                logger.debug("Skipping observation with empty content: %s", payload)
                return None

            return EngramObservation(
                obs_id=str(
                    payload.get("sync_id")
                    or payload.get("id")
                    or ""
                ),
                session_id=str(payload.get("session_id") or ""),
                group_id=str(payload.get("project") or self.project),
                obs_type=str(payload.get("type") or payload.get("obs_type") or "fact"),
                content=content,
                timestamp=ts,
                confidence=float(payload.get("confidence", 1.0)),
                metadata=dict(payload.get("metadata") or {}),
            )
        except Exception as exc:
            logger.warning("Skipping malformed cloud_mutations payload: %s — %s", payload, exc)
            return None


# ── Multi-project helpers ─────────────────────────────────────────────────────

def list_projects(dsn: str = ENGRAM_PG_DSN) -> list[str]:
    """Return all distinct projects in cloud_mutations.

    Used for multi-tenant iteration when no explicit allowlist is configured.
    Raises EngramPostgresReaderError on connection failure.
    """
    try:
        import psycopg
    except ImportError:
        raise EngramPostgresReaderError(
            "psycopg is not installed — add psycopg[binary] to requirements.txt"
        )

    if not dsn:
        raise EngramPostgresReaderError("ENGRAM_PG_DSN is not set")

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT project FROM cloud_mutations WHERE entity = 'observation' ORDER BY project"
                )
                rows = cur.fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        raise EngramPostgresReaderError(
            f"Failed to list Engram projects from Postgres: {exc}"
        ) from exc
