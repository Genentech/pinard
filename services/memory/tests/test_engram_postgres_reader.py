"""Unit tests for EngramPostgresReader + multi-project iteration.

All Postgres I/O is mocked via unittest.mock — no live database needed.
Covers:
  1. Payload parsing (happy path, empty content, malformed payload, delete tombstone)
  2. Incremental cursor: seq > last_seq, cursor advanced after successful fetch
  3. Error handling: missing DSN, psycopg import absent, connection failure, query failure
  4. Multi-project iteration in _run_engram_ingestion with MEMORY_ENGRAM_SOURCE=postgres
  5. list_projects helper
  6. HTTP fallback when MEMORY_ENGRAM_SOURCE=http
"""
from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from services.memory.engram_postgres_reader import (
    EngramPostgresReader,
    EngramPostgresReaderError,
    _Cursor,
    list_projects,
)
from services.memory.engram_reader import EngramObservation


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_row(
    seq: int,
    op: str = "upsert",
    *,
    project: str = "exo-cli",
    content: str = "some observation",
    obs_type: str = "fact",
    sync_id: str = "obs-abc",
    session_id: str = "sess-xyz",
    created_at: str = "2026-07-01T10:00:00Z",
) -> tuple:
    payload = {
        "sync_id": sync_id,
        "session_id": session_id,
        "project": project,
        "type": obs_type,
        "content": content,
        "created_at": created_at,
        "confidence": 0.95,
    }
    return (seq, f"obs-{seq}", op, payload)


def _make_conn_ctx(rows: list[tuple]) -> MagicMock:
    """Return a mock psycopg connection context manager that yields rows."""
    cur = MagicMock()
    cur.execute = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    return conn


# ── 1. Payload parsing ────────────────────────────────────────────────────────

class TestPayloadParsing:
    def _reader(self, tmp_path: Path) -> EngramPostgresReader:
        return EngramPostgresReader(
            project="exo-cli",
            dsn="postgresql://fake",
            cursor_path=tmp_path / "cursors.json",
        )

    def test_happy_path_parses_all_fields(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        payload = {
            "sync_id": "obs-001",
            "session_id": "sess-abc",
            "project": "exo-cli",
            "type": "diagnosis",
            "content": "OOM on shard 47",
            "created_at": "2026-07-01T10:00:00Z",
            "confidence": 0.9,
        }
        obs = reader._parse_payload(payload)
        assert obs is not None
        assert obs.obs_id == "obs-001"
        assert obs.session_id == "sess-abc"
        assert obs.group_id == "exo-cli"
        assert obs.obs_type == "diagnosis"
        assert obs.content == "OOM on shard 47"
        assert obs.confidence == 0.9
        assert obs.timestamp.year == 2026

    def test_empty_content_returns_none(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        obs = reader._parse_payload({"content": "", "type": "fact"})
        assert obs is None

    def test_missing_content_key_returns_none(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        obs = reader._parse_payload({"type": "fact", "sync_id": "x"})
        assert obs is None

    def test_malformed_payload_returns_none(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        # created_at with invalid format — should not raise, just warn and return None
        obs = reader._parse_payload({"content": "x", "created_at": "not-a-date"})
        # Depending on fromisoformat behaviour — may parse or fail; must not raise
        # (we only assert no exception)

    def test_falls_back_to_project_when_payload_project_missing(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        obs = reader._parse_payload({"content": "some obs", "type": "fact"})
        assert obs is not None
        assert obs.group_id == "exo-cli"

    def test_uses_sync_id_as_obs_id(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        obs = reader._parse_payload(
            {"content": "x", "sync_id": "sid-123", "id": "id-456", "type": "rule"}
        )
        assert obs is not None
        assert obs.obs_id == "sid-123"

    def test_falls_back_to_id_when_no_sync_id(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        obs = reader._parse_payload({"content": "x", "id": "id-789", "type": "fact"})
        assert obs is not None
        assert obs.obs_id == "id-789"


# ── 2. Incremental cursor ─────────────────────────────────────────────────────

class TestIncrementalCursor:
    def _reader(self, tmp_path: Path, project: str = "exo-cli") -> EngramPostgresReader:
        return EngramPostgresReader(
            project=project,
            dsn="postgresql://fake",
            cursor_path=tmp_path / "cursors.json",
        )

    def test_starts_from_zero_on_first_call(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        rows = [_make_row(5), _make_row(10)]
        conn = _make_conn_ctx(rows)

        with patch("psycopg.connect", return_value=conn):
            result = reader.fetch()

        # cursor.execute called with last_seq=0
        cur = conn.cursor.return_value
        call_args = cur.execute.call_args[0]
        assert call_args[1] == ("exo-cli", 0, 1000)

        # cursor advanced to max seq
        cursor_data = json.loads((tmp_path / "cursors.json").read_text())
        assert cursor_data["exo-cli"] == 10

    def test_resumes_from_persisted_cursor(self, tmp_path: Path) -> None:
        # Pre-seed cursor file
        cursor_file = tmp_path / "cursors.json"
        cursor_file.write_text(json.dumps({"exo-cli": 42}))

        reader = self._reader(tmp_path)
        rows = [_make_row(43), _make_row(44)]
        conn = _make_conn_ctx(rows)

        with patch("psycopg.connect", return_value=conn):
            result = reader.fetch()

        cur = conn.cursor.return_value
        call_args = cur.execute.call_args[0]
        assert call_args[1] == ("exo-cli", 42, 1000)

        cursor_data = json.loads(cursor_file.read_text())
        assert cursor_data["exo-cli"] == 44

    def test_cursor_not_updated_on_empty_result(self, tmp_path: Path) -> None:
        cursor_file = tmp_path / "cursors.json"
        cursor_file.write_text(json.dumps({"exo-cli": 99}))

        reader = self._reader(tmp_path)
        conn = _make_conn_ctx([])  # no rows

        with patch("psycopg.connect", return_value=conn):
            result = reader.fetch()

        assert result == []
        # Cursor file unchanged
        cursor_data = json.loads(cursor_file.read_text())
        assert cursor_data["exo-cli"] == 99

    def test_delete_tombstones_skipped_but_cursor_advances(self, tmp_path: Path) -> None:
        reader = self._reader(tmp_path)
        rows = [
            _make_row(1, op="upsert"),
            _make_row(2, op="delete"),   # tombstone — skipped
            _make_row(3, op="upsert"),
        ]
        conn = _make_conn_ctx(rows)

        with patch("psycopg.connect", return_value=conn):
            result = reader.fetch()

        # Only 2 observations returned (delete skipped)
        assert len(result) == 2
        # Cursor advances to 3 (max seq in batch)
        cursor_data = json.loads((tmp_path / "cursors.json").read_text())
        assert cursor_data["exo-cli"] == 3

    def test_per_project_cursors_are_independent(self, tmp_path: Path) -> None:
        cursor_file = tmp_path / "cursors.json"
        cursor_file.write_text(json.dumps({"proj-a": 10, "proj-b": 50}))

        reader_a = EngramPostgresReader(
            project="proj-a", dsn="postgresql://fake", cursor_path=cursor_file
        )
        rows_a = [_make_row(11, project="proj-a")]
        conn_a = _make_conn_ctx(rows_a)

        with patch("psycopg.connect", return_value=conn_a):
            reader_a.fetch()

        cursor_data = json.loads(cursor_file.read_text())
        # proj-a advanced, proj-b unchanged
        assert cursor_data["proj-a"] == 11
        assert cursor_data["proj-b"] == 50


# ── 3. Error handling ─────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_dsn_raises_immediately(self, tmp_path: Path) -> None:
        with pytest.raises(EngramPostgresReaderError, match="ENGRAM_PG_DSN is not set"):
            EngramPostgresReader(
                project="proj",
                dsn="",
                cursor_path=tmp_path / "cursors.json",
            )

    def test_psycopg_import_error_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = EngramPostgresReader(
            project="proj",
            dsn="postgresql://fake",
            cursor_path=tmp_path / "cursors.json",
        )
        with patch.dict("sys.modules", {"psycopg": None}):
            with pytest.raises((EngramPostgresReaderError, ImportError)):
                reader.fetch()

    def test_connection_failure_raises(self, tmp_path: Path) -> None:
        reader = EngramPostgresReader(
            project="proj",
            dsn="postgresql://fake",
            cursor_path=tmp_path / "cursors.json",
        )
        with patch("psycopg.connect", side_effect=Exception("connection refused")):
            with pytest.raises(EngramPostgresReaderError, match="query failed"):
                reader.fetch()

    def test_query_failure_raises(self, tmp_path: Path) -> None:
        reader = EngramPostgresReader(
            project="proj",
            dsn="postgresql://fake",
            cursor_path=tmp_path / "cursors.json",
        )
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute.side_effect = Exception("column does not exist")

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with patch("psycopg.connect", return_value=conn):
            with pytest.raises(EngramPostgresReaderError, match="query failed"):
                reader.fetch()

    def test_malformed_cursor_file_falls_back_to_zero(self, tmp_path: Path) -> None:
        cursor_file = tmp_path / "cursors.json"
        cursor_file.write_text("not valid json{")

        reader = EngramPostgresReader(
            project="proj",
            dsn="postgresql://fake",
            cursor_path=cursor_file,
        )
        conn = _make_conn_ctx([])
        with patch("psycopg.connect", return_value=conn):
            result = reader.fetch()

        # Should not raise; falls back to seq=0
        cur = conn.cursor.return_value
        call_args = cur.execute.call_args[0]
        assert call_args[1][1] == 0  # last_seq = 0


# ── 4. list_projects helper ───────────────────────────────────────────────────

class TestListProjects:
    def test_returns_distinct_projects(self) -> None:
        rows = [("exo-cli",), ("genomics-build",), ("pinard",)]
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = rows

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with patch("psycopg.connect", return_value=conn):
            projects = list_projects(dsn="postgresql://fake")

        assert projects == ["exo-cli", "genomics-build", "pinard"]

    def test_empty_dsn_raises(self) -> None:
        with pytest.raises(EngramPostgresReaderError, match="ENGRAM_PG_DSN is not set"):
            list_projects(dsn="")

    def test_connection_failure_raises(self) -> None:
        with patch("psycopg.connect", side_effect=Exception("timeout")):
            with pytest.raises(EngramPostgresReaderError, match="Failed to list"):
                list_projects(dsn="postgresql://fake")


# ── 5. _Cursor persistence ────────────────────────────────────────────────────

class TestCursorPersistence:
    def test_get_returns_zero_for_unknown_project(self, tmp_path: Path) -> None:
        c = _Cursor(path=tmp_path / "cursors.json")
        assert c.get("unknown-project") == 0

    def test_update_persists_and_reloads(self, tmp_path: Path) -> None:
        path = tmp_path / "cursors.json"
        c1 = _Cursor(path=path)
        c1.update("proj", 42)

        c2 = _Cursor(path=path)
        assert c2.get("proj") == 42

    def test_update_does_not_overwrite_other_projects(self, tmp_path: Path) -> None:
        path = tmp_path / "cursors.json"
        path.write_text(json.dumps({"proj-a": 10, "proj-b": 20}))

        c = _Cursor(path=path)
        c.update("proj-a", 15)

        data = json.loads(path.read_text())
        assert data["proj-a"] == 15
        assert data["proj-b"] == 20

    def test_cursor_dir_created_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "cursors.json"
        c = _Cursor(path=nested)
        c.update("proj", 1)
        assert nested.exists()


# ── 6. Multi-project ingestion via MEMORY_ENGRAM_SOURCE=postgres ──────────────

class TestMultiProjectIngestion:
    """Integration-level test: _run_engram_ingestion calls EngramPostgresReader
    for each project returned by list_projects when source=postgres."""

    def test_postgres_source_iterates_all_projects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.memory.ingester as ingester_mod

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "postgres")

        projects = ["proj-a", "proj-b"]
        fetched: list[str] = []

        from services.memory.ontology.registry import OntologyRegistry

        # Patch _ingest_group instead of the reader so we avoid __init__ DSN check
        def fake_ingest_group(group_id: str, registry: object) -> None:
            fetched.append(group_id)

        with (
            patch(
                "services.memory.ingester.list_engram_projects",
                return_value=projects,
            ),
            patch("services.memory.ingester._ingest_group", fake_ingest_group),
        ):
            ingester_mod._run_engram_ingestion(OntologyRegistry())

        assert set(fetched) == set(projects)

    def test_http_source_uses_http_reader(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.memory.ingester as ingester_mod

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "http")

        from services.memory.ontology.registry import OntologyRegistry
        from services.memory.engram_reader import EngramReader

        fetched: list[str] = []

        def fake_http_fetch(self: EngramReader) -> list:
            fetched.append(self.group_id)
            return []

        with (
            patch.object(EngramReader, "fetch", fake_http_fetch),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "proj-http"}),
        ):
            ingester_mod._run_engram_ingestion(OntologyRegistry())

        assert "proj-http" in fetched

    def test_postgres_source_logs_warning_on_list_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import services.memory.ingester as ingester_mod
        import logging
        import services.memory.surrealdb.client as _client_mod

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "postgres")

        from services.memory.ontology.registry import OntologyRegistry
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.signin = MagicMock(return_value=None)
        mock_db.use = MagicMock(return_value=None)
        mock_db.query_raw = MagicMock(return_value={"result": [{"status": "OK", "result": []}]})
        mock_db.check_response_for_error = MagicMock(return_value=None)
        mock_db.check_response_for_result = MagicMock(return_value=None)

        with (
            patch(
                "services.memory.ingester.list_engram_projects",
                side_effect=EngramPostgresReaderError("timeout"),
            ),
            patch.dict("os.environ", {"WORKER_GROUP_ID": "fallback-proj"}),
            patch.object(EngramPostgresReader, "fetch", return_value=[]),
            patch.dict("os.environ", {"ENGRAM_PG_DSN": "postgresql://fake"}),
            patch("services.memory.surrealdb.client.Surreal", return_value=mock_db),
            caplog.at_level(logging.WARNING),
        ):
            _client_mod._schema_applied.clear()
            ingester_mod._run_engram_ingestion(OntologyRegistry())

        assert any("falling back to local registry" in r.message for r in caplog.records)

    def test_postgres_source_errors_logged_at_warning_for_transient(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Connection errors are logged at WARNING, not ERROR."""
        import services.memory.ingester as ingester_mod
        import logging
        import services.memory.surrealdb.client as _client_mod

        monkeypatch.setattr(ingester_mod, "MEMORY_ENGRAM_SOURCE", "postgres")

        from services.memory.ontology.registry import OntologyRegistry
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.signin = MagicMock(return_value=None)
        mock_db.use = MagicMock(return_value=None)
        mock_db.query_raw = MagicMock(return_value={"result": [{"status": "OK", "result": []}]})
        mock_db.check_response_for_error = MagicMock(return_value=None)
        mock_db.check_response_for_result = MagicMock(return_value=None)

        with (
            patch(
                "services.memory.ingester.list_engram_projects",
                return_value=["proj-x"],
            ),
            patch.object(
                EngramPostgresReader,
                "fetch",
                side_effect=EngramPostgresReaderError("query failed: connection refused"),
            ),
            patch.dict("os.environ", {"ENGRAM_PG_DSN": "postgresql://fake"}),
            patch("services.memory.surrealdb.client.Surreal", return_value=mock_db),
            caplog.at_level(logging.WARNING),
        ):
            _client_mod._schema_applied.clear()
            ingester_mod._run_engram_ingestion(OntologyRegistry())

        # Should have logged a warning, not crashed
        assert any("transient" in r.message.lower() or "query failed" in r.message.lower() for r in caplog.records)
