"""Unit tests for the durable SurrealDB ingest cursor.

Tests:
  1. SurrealClient.get_ingest_cursor returns 0 for an unknown source (first call)
  2. SurrealClient.set_ingest_cursor persists the seq
  3. SurrealClient.get_ingest_cursor returns the persisted seq after set_ingest_cursor
  4. SurrealCursorStore.get / update delegate correctly to SurrealClient methods
  5. ingest_cursor DDL is present in schema_gen _BASE_DDL
  6. CursorStore protocol is satisfied by both _Cursor and SurrealCursorStore
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import services.memory.surrealdb.client as _client_mod
from services.memory.surrealdb.schema_gen import _BASE_DDL
from services.memory.engram_postgres_reader import CursorStore, _Cursor
from services.memory.ingester import SurrealCursorStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_db() -> MagicMock:
    db = MagicMock()
    db.__enter__ = MagicMock(return_value=db)
    db.__exit__ = MagicMock(return_value=False)
    db.signin = MagicMock(return_value=None)
    db.use = MagicMock(return_value=None)
    db.query_raw = MagicMock(return_value={"result": [{"status": "OK", "result": []}]})
    db.check_response_for_error = MagicMock(return_value=None)
    db.check_response_for_result = MagicMock(return_value=None)
    return db


def _make_client(group_id: str = "exo-cli", mock_db: MagicMock | None = None):
    _client_mod._schema_applied.discard(group_id)
    if mock_db is None:
        mock_db = _make_mock_db()
    with patch("services.memory.surrealdb.client.Surreal", return_value=mock_db):
        from services.memory.surrealdb.client import SurrealClient
        client = SurrealClient(group_id=group_id, url="http://localhost:8000",
                               user="root", password="secret")
    return client, mock_db


# ---------------------------------------------------------------------------
# 1. get_ingest_cursor returns 0 for unknown source
# ---------------------------------------------------------------------------

class TestGetIngestCursorDefault:
    def test_returns_zero_when_no_record(self) -> None:
        client, mock_db = _make_client()
        # SELECT VALUE returns a list of raw scalar values; empty when record absent
        mock_db.query_raw.return_value = {
            "result": [{"status": "OK", "result": []}]
        }
        result = client.get_ingest_cursor("engram_pg:exo-cli")
        assert result == 0

    def test_returns_zero_when_result_empty(self) -> None:
        client, mock_db = _make_client()
        mock_db.query_raw.return_value = {
            "result": [{"status": "OK", "result": []}]
        }
        result = client.get_ingest_cursor("engram_pg:exo-cli")
        assert result == 0

    def test_returns_seq_when_record_exists(self) -> None:
        client, mock_db = _make_client()
        # SELECT VALUE seq returns [42] (a list of the scalar seq values)
        mock_db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [42]}]
        }
        result = client.get_ingest_cursor("engram_pg:exo-cli")
        assert result == 42


# ---------------------------------------------------------------------------
# 2 & 3. set_ingest_cursor persists seq; get returns it
# ---------------------------------------------------------------------------

class TestSetIngestCursor:
    def test_set_calls_query_with_correct_params(self) -> None:
        client, mock_db = _make_client()
        mock_db.query_raw.return_value = {
            "result": [{"status": "OK", "result": []}]
        }
        client.set_ingest_cursor("engram_pg:pinard", 6667)
        # The query_raw must have been called with seq=6667 in vars
        call_args = mock_db.query_raw.call_args
        sql_arg = call_args[0][0]
        vars_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("vars", {})
        assert vars_arg.get("seq") == 6667
        assert vars_arg.get("source") == "engram_pg:pinard"

    def test_get_after_set_returns_persisted_seq(self) -> None:
        client, mock_db = _make_client()

        # First call: set_ingest_cursor
        mock_db.query_raw.return_value = {
            "result": [{"status": "OK", "result": []}]
        }
        client.set_ingest_cursor("engram_pg:exo-cli", 1234)

        # Second call: get_ingest_cursor — SELECT VALUE returns raw scalar list
        mock_db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [1234]}]
        }
        seq = client.get_ingest_cursor("engram_pg:exo-cli")
        assert seq == 1234


# ---------------------------------------------------------------------------
# 4. SurrealCursorStore delegates to SurrealClient methods
# ---------------------------------------------------------------------------

class TestSurrealCursorStore:
    def _make_store(self) -> tuple[SurrealCursorStore, MagicMock]:
        from services.memory.surrealdb.client import SurrealClient
        surreal = MagicMock(spec=SurrealClient)
        surreal.get_ingest_cursor.return_value = 0
        store = SurrealCursorStore(surreal, source="engram_pg")
        return store, surreal

    def test_get_delegates_to_client(self) -> None:
        store, surreal = self._make_store()
        surreal.get_ingest_cursor.return_value = 42
        result = store.get("my-project")
        surreal.get_ingest_cursor.assert_called_once_with("engram_pg:my-project")
        assert result == 42

    def test_update_delegates_to_client(self) -> None:
        store, surreal = self._make_store()
        store.update("my-project", 999)
        surreal.set_ingest_cursor.assert_called_once_with("engram_pg:my-project", 999)

    def test_get_returns_zero_default(self) -> None:
        store, surreal = self._make_store()
        surreal.get_ingest_cursor.return_value = 0
        assert store.get("unknown") == 0

    def test_key_includes_source_and_project(self) -> None:
        from services.memory.surrealdb.client import SurrealClient
        surreal = MagicMock(spec=SurrealClient)
        store = SurrealCursorStore(surreal, source="engram_pg")
        store.get("proj-x")
        surreal.get_ingest_cursor.assert_called_once_with("engram_pg:proj-x")


# ---------------------------------------------------------------------------
# 5. ingest_cursor DDL present in schema_gen _BASE_DDL
# ---------------------------------------------------------------------------

class TestSchemaGenIngestCursor:
    def test_ingest_cursor_table_defined(self) -> None:
        assert "ingest_cursor" in _BASE_DDL

    def test_ingest_cursor_has_source_field(self) -> None:
        assert "source" in _BASE_DDL
        assert "ON ingest_cursor TYPE string" in _BASE_DDL

    def test_ingest_cursor_has_seq_field(self) -> None:
        assert "ON ingest_cursor TYPE int" in _BASE_DDL

    def test_ingest_cursor_has_unique_index(self) -> None:
        assert "ingest_cursor_source" in _BASE_DDL


# ---------------------------------------------------------------------------
# 6. CursorStore protocol satisfied by _Cursor and SurrealCursorStore
# ---------------------------------------------------------------------------

class TestCursorStoreProtocol:
    def test_file_cursor_satisfies_protocol(self, tmp_path: Path) -> None:
        c = _Cursor(path=tmp_path / "cursors.json")
        assert isinstance(c, CursorStore)

    def test_surreal_cursor_store_satisfies_protocol(self) -> None:
        from services.memory.surrealdb.client import SurrealClient
        surreal = MagicMock(spec=SurrealClient)
        store = SurrealCursorStore(surreal)
        assert isinstance(store, CursorStore)
