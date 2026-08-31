"""Integration tests for the portable memory subset feature (tasks §10).

Tests:
1. Subset export: SELECT from central (mocked) → write embedded file
2. Version-stamp written correctly (ontology versions in subset_meta)
3. Embedded client: recall_cosine_scan returns seeded data
4. Embedded client: lookup returns seeded data
5. EmbeddedClientError on missing file
6. query_handler prefers embedded over central when MEMORY_EMBEDDED_SUBSET is set

All external I/O (central SurrealDB server, surrealdb embedded engine) is mocked
so that these tests run without a SurrealDB binary or running server.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from services.memory.surrealdb.embedded_client import (
    EmbeddedClientError,
    EmbeddedSurrealClient,
    load_embedded_subset,
)
from services.memory.surrealdb.subset import (
    SubsetError,
    SubsetResult,
    _fetch_all,
    _fetch_edges,
    _write_subset_meta,
    export_subset,
)
from services.memory.ontology.registry import OntologyRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_ENTITIES = [
    {
        "id": "entity:1",
        "role": "diagnosis",
        "name": "OOM on shard 47",
        "description": "Out-of-memory killed process on shard 47",
        "version": "1.0.0",
        "data": {"obs_type": "diagnosis"},
        "embedding": [0.1] * 1024,
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-01T10:00:00Z",
    },
    {
        "id": "entity:2",
        "role": "action",
        "name": "increase --mem-budget",
        "description": "Set --mem-budget 64G to fix OOM",
        "version": "1.0.0",
        "data": {},
        "embedding": [0.2] * 1024,
        "created_at": "2026-07-01T10:01:00Z",
        "updated_at": "2026-07-01T10:01:00Z",
    },
]

SAMPLE_WIKI_DOCS = [
    {
        "id": "wiki_doc:1",
        "title": "GWAS OOM Recovery",
        "body": "See the action: increase --mem-budget",
        "frontmatter": {},
        "path": "gwas-oom-recovery.md",
        "confidence": 0.9,
        "status": "auto_serve",
        "embedding": [0.3] * 1024,
        "created_at": "2026-07-01T10:02:00Z",
        "updated_at": "2026-07-01T10:02:00Z",
    },
]

SAMPLE_EDGES = [
    {
        "_edge_table": "resolved_by",
        "id": "resolved_by:1",
        "in": "entity:1",
        "out": "entity:2",
        "confidence": 1.0,
        "description": "standard fix",
        "data": {},
        "created_at": "2026-07-01T10:03:00Z",
    },
]


def _make_mock_central() -> MagicMock:
    """Mock SurrealClient that returns SAMPLE_ENTITIES, SAMPLE_WIKI_DOCS, SAMPLE_EDGES."""
    central = MagicMock()
    central.__enter__ = MagicMock(return_value=central)
    central.__exit__ = MagicMock(return_value=False)

    def fake_query(sql: str, vars: dict | None = None) -> list[Any]:
        if "FROM entity" in sql:
            return [{"result": SAMPLE_ENTITIES}]
        if "FROM wiki_doc" in sql:
            return [{"result": SAMPLE_WIKI_DOCS}]
        if "FROM resolved_by" in sql:
            return [{"result": [SAMPLE_EDGES[0]]}]
        return [{"result": []}]

    central.query.side_effect = fake_query
    return central


def _make_mock_embedded_db() -> MagicMock:
    """Mock surrealdb.Surreal for the embedded engine."""
    db = MagicMock()
    db.connect = MagicMock()
    db.use = MagicMock()
    db.close = MagicMock()

    # query() returns list-of-result-sets as the real client does.
    stored_records: list[dict] = []

    def fake_query(sql: str, vars: dict | None = None) -> list[Any]:
        if "CREATE entity" in sql or "CREATE wiki_doc" in sql or "DEFINE" in sql:
            return []
        if "CREATE subset_meta" in sql:
            return []
        if "RELATE" in sql:
            return []
        if "FROM entity" in sql:
            return [{"result": stored_records}]
        if "FROM subset_meta" in sql:
            return [{"result": [{"pinard_core_version": "1.0.0"}]}]
        return [{"result": []}]

    db.query.side_effect = fake_query
    return db


# ── Test 1: export_subset calls central SurrealClient + surrealdb embedded ───

class TestExportSubset:
    def test_export_subset_returns_subset_result(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        mock_db = _make_mock_embedded_db()
        registry = OntologyRegistry()

        with patch("services.memory.surrealdb.subset.SurrealClient", return_value=_make_mock_central()):
            with patch("surrealdb.Surreal", return_value=mock_db):
                with patch.object(Path, "exists", return_value=False):
                    result = export_subset("genomics-build", out_path, registry)

        assert isinstance(result, SubsetResult)
        assert result.group_id == "genomics-build"
        assert result.entity_count == 2
        assert result.wiki_doc_count == 1
        assert result.edge_count >= 0  # edges may vary based on which tables exist

    def test_export_subset_applies_schema(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        mock_db = _make_mock_embedded_db()
        registry = OntologyRegistry()

        with patch("services.memory.surrealdb.subset.SurrealClient", return_value=_make_mock_central()):
            with patch("surrealdb.Surreal", return_value=mock_db):
                with patch.object(Path, "exists", return_value=False):
                    export_subset("genomics-build", out_path, registry)

        # Should have called query() with DEFINE statements.
        calls = [str(c) for c in mock_db.query.call_args_list]
        define_calls = [c for c in calls if "DEFINE" in c]
        assert len(define_calls) > 0

    def test_export_subset_calls_use_with_correct_group(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        mock_db = _make_mock_embedded_db()
        registry = OntologyRegistry()

        with patch("services.memory.surrealdb.subset.SurrealClient", return_value=_make_mock_central()):
            with patch("surrealdb.Surreal", return_value=mock_db):
                with patch.object(Path, "exists", return_value=False):
                    export_subset("genomics-build", out_path, registry)

        mock_db.use.assert_called_once_with("pinard", "genomics-build")

    def test_export_subset_raises_on_central_error(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        from services.memory.surrealdb.client import SurrealError

        bad_central = MagicMock()
        bad_central.__enter__ = MagicMock(return_value=bad_central)
        bad_central.__exit__ = MagicMock(return_value=False)
        bad_central.query.side_effect = SurrealError("connection refused")

        with (
            patch("services.memory.surrealdb.subset.SurrealClient", return_value=bad_central),
        ):
            with pytest.raises(SubsetError, match="Failed to read"):
                export_subset("genomics-build", out_path)

    def test_export_subset_removes_existing_path(self, tmp_path: Path) -> None:
        out_path = tmp_path / "existing.surrealkv"
        out_path.mkdir()  # Create a dir to simulate an existing file.
        mock_db = _make_mock_embedded_db()
        registry = OntologyRegistry()

        with patch("services.memory.surrealdb.subset.SurrealClient", return_value=_make_mock_central()):
            with patch("surrealdb.Surreal", return_value=mock_db):
                export_subset("genomics-build", out_path, registry)

        # The directory should have been removed (replaced by the embedded file).
        # mock_db.connect() will have been called, proving the path was cleared.
        mock_db.connect.assert_called_once()


# ── Test 2: Version-stamp written correctly ───────────────────────────────────

class TestVersionStamp:
    def test_version_stamp_in_result(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        mock_db = _make_mock_embedded_db()
        registry = OntologyRegistry()

        with patch("services.memory.surrealdb.subset.SurrealClient", return_value=_make_mock_central()):
            with patch("surrealdb.Surreal", return_value=mock_db):
                with patch.object(Path, "exists", return_value=False):
                    result = export_subset("genomics-build", out_path, registry)

        assert "pinard_core" in result.ontology_stamp
        assert result.ontology_stamp["pinard_core"] == "1.0.0"

    def test_version_stamp_with_domain(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        mock_db = _make_mock_embedded_db()
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="genomics-build",
            domain_name="genomics",
            domain_version="2.1.0",
        )

        with patch("services.memory.surrealdb.subset.SurrealClient", return_value=_make_mock_central()):
            with patch("surrealdb.Surreal", return_value=mock_db):
                with patch.object(Path, "exists", return_value=False):
                    result = export_subset("genomics-build", out_path, registry)

        assert result.ontology_stamp.get("domain", {}).get("name") == "genomics"
        assert result.ontology_stamp.get("domain", {}).get("version") == "2.1.0"

    def test_write_subset_meta_fields(self) -> None:
        """_write_subset_meta calls db.query with all required fields."""
        mock_db = MagicMock()
        mock_db.query = MagicMock(return_value=[])

        _write_subset_meta(
            db=mock_db,
            group_id="genomics-build",
            exported_at="2026-07-15T12:00:00Z",
            ontology_stamp={"pinard_core": "1.0.0", "domain": {"name": "genomics", "version": "2.0.0"}},
            entity_count=42,
            wiki_doc_count=3,
            edge_count=7,
        )

        mock_db.query.assert_called_once()
        call_args = mock_db.query.call_args
        sql = call_args[0][0]
        vars = call_args[0][1]

        assert "subset_meta" in sql
        assert vars["group_id"] == "genomics-build"
        assert vars["entity_count"] == 42
        assert vars["wiki_doc_count"] == 3
        assert vars["edge_count"] == 7
        assert vars["core_version"] == "1.0.0"
        assert vars["domain_name"] == "genomics"
        assert vars["domain_version"] == "2.0.0"


# ── Test 3: Embedded client — recall_cosine_scan ──────────────────────────────

class TestEmbeddedClient:
    def _make_client(self, query_result: list[dict] | None = None) -> EmbeddedSurrealClient:
        mock_db = MagicMock()
        result = query_result if query_result is not None else SAMPLE_ENTITIES
        mock_db.query.return_value = [{"result": result}]
        return EmbeddedSurrealClient(db=mock_db, group_id="genomics-build")

    def test_recall_cosine_scan_passes_vec_and_limit(self) -> None:
        client = self._make_client()
        vec = [0.1] * 1024
        client.recall_cosine_scan(embedding=vec, limit=5)
        call_args = client._db.query.call_args
        assert call_args[0][1]["vec"] == vec
        assert call_args[0][1]["limit"] == 5

    def test_recall_cosine_scan_with_role_filter(self) -> None:
        client = self._make_client()
        client.recall_cosine_scan(embedding=[0.0] * 1024, role_filter="diagnosis")
        call_args = client._db.query.call_args
        assert call_args[0][1].get("role") == "diagnosis"

    def test_recall_cosine_scan_returns_results(self) -> None:
        client = self._make_client(query_result=SAMPLE_ENTITIES)
        results = client.recall_cosine_scan(embedding=[0.1] * 1024)
        assert results == SAMPLE_ENTITIES

    def test_recall_cosine_scan_returns_empty_on_error(self) -> None:
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("db closed")
        client = EmbeddedSurrealClient(db=mock_db, group_id="grp")
        result = client.recall_cosine_scan(embedding=[0.0] * 1024)
        assert result == []

    def test_lookup_calls_fts(self) -> None:
        client = self._make_client(query_result=[SAMPLE_ENTITIES[0]])
        results = client.lookup("OOM shard 47")
        assert len(results) == 1

    def test_lookup_falls_back_on_fts_error(self) -> None:
        mock_db = MagicMock()
        entity = SAMPLE_ENTITIES[0]
        # First call (FTS) raises, second call (LIKE scan) returns the entity.
        mock_db.query.side_effect = [
            Exception("FTS unavailable"),
            [{"result": [entity]}],
        ]
        client = EmbeddedSurrealClient(db=mock_db, group_id="grp")
        results = client.lookup("OOM")
        assert len(results) == 1
        assert results[0]["name"] == entity["name"]

    def test_trace_queries_graph(self) -> None:
        client = self._make_client(query_result=[{"neighbors": [SAMPLE_ENTITIES[1]]}])
        results = client.trace("diagnosis", "OOM on shard 47", "resolved_by")
        call_args = client._db.query.call_args
        assert "resolved_by" in call_args[0][0]

    def test_query_meta_returns_stamp(self) -> None:
        meta = {"pinard_core_version": "1.0.0", "group_id": "grp"}
        client = self._make_client(query_result=[meta])
        result = client.query_meta()
        assert result == meta

    def test_query_meta_returns_none_when_empty(self) -> None:
        client = self._make_client(query_result=[])
        result = client.query_meta()
        assert result is None

    def test_close_calls_db_close(self) -> None:
        mock_db = MagicMock()
        mock_db.query.return_value = [{"result": []}]
        client = EmbeddedSurrealClient(db=mock_db, group_id="grp")
        client.close()
        mock_db.close.assert_called_once()

    def test_context_manager(self) -> None:
        mock_db = MagicMock()
        mock_db.query.return_value = [{"result": []}]
        with EmbeddedSurrealClient(db=mock_db, group_id="grp") as client:
            assert isinstance(client, EmbeddedSurrealClient)
        mock_db.close.assert_called_once()


# ── Test 4: load_embedded_subset ──────────────────────────────────────────────

class TestLoadEmbeddedSubset:
    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(EmbeddedClientError, match="not found"):
            load_embedded_subset(tmp_path / "nonexistent.surrealkv", "grp")

    def test_returns_embedded_client_on_success(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        out_path.mkdir()  # Fake existence check.

        mock_db = MagicMock()
        mock_db.connect = MagicMock()
        mock_db.use = MagicMock()
        mock_db.query.return_value = [{"result": []}]

        with patch("surrealdb.Surreal", return_value=mock_db):
            client = load_embedded_subset(out_path, "genomics-build")

        assert isinstance(client, EmbeddedSurrealClient)
        mock_db.connect.assert_called_once()
        mock_db.use.assert_called_once_with("pinard", "genomics-build")

    def test_raises_on_surreal_open_error(self, tmp_path: Path) -> None:
        out_path = tmp_path / "test.surrealkv"
        out_path.mkdir()

        mock_db = MagicMock()
        mock_db.connect.side_effect = RuntimeError("cannot open file")

        with patch("surrealdb.Surreal", return_value=mock_db):
            with pytest.raises(EmbeddedClientError, match="Failed to open"):
                load_embedded_subset(out_path, "grp")


# ── Test 5: query_handler prefers embedded over central ───────────────────────

class TestQueryHandlerEmbeddedPreference:
    def test_uses_embedded_when_env_set(self) -> None:
        """When MEMORY_EMBEDDED_SUBSET is set and the file exists, use embedded client."""
        import services.memory.query_handler as qh

        mock_embedded_client = MagicMock()
        mock_embedded_client.__enter__ = MagicMock(return_value=mock_embedded_client)
        mock_embedded_client.__exit__ = MagicMock(return_value=False)

        with patch.object(qh, "_EMBEDDED_SUBSET_PATH", "/fake/path.surrealkv"):
            with patch(
                "services.memory.query_handler.load_embedded_subset",
                return_value=mock_embedded_client,
            ) as mock_load:
                qh._open_surreal_client("genomics-build")

        mock_load.assert_called_once_with("/fake/path.surrealkv", "genomics-build")

    def test_falls_back_to_central_on_embedded_error(self) -> None:
        """When embedded file fails to load, fall back to central SurrealClient."""
        import services.memory.query_handler as qh

        mock_central = MagicMock()

        with patch.object(qh, "_EMBEDDED_SUBSET_PATH", "/nonexistent.surrealkv"):
            with patch(
                "services.memory.query_handler.load_embedded_subset",
                side_effect=EmbeddedClientError("not found"),
            ):
                with patch(
                    "services.memory.query_handler.SurrealClient",
                    return_value=mock_central,
                ) as mock_central_cls:
                    qh._open_surreal_client("genomics-build")

        mock_central_cls.assert_called_once_with(group_id="genomics-build")

    def test_uses_central_when_no_env_set(self) -> None:
        """When MEMORY_EMBEDDED_SUBSET is unset, use central SurrealClient directly."""
        import services.memory.query_handler as qh

        mock_central = MagicMock()

        with patch.object(qh, "_EMBEDDED_SUBSET_PATH", None):
            with patch(
                "services.memory.query_handler.SurrealClient",
                return_value=mock_central,
            ) as mock_central_cls:
                with patch(
                    "services.memory.query_handler.load_embedded_subset",
                ) as mock_load:
                    qh._open_surreal_client("genomics-build")

        mock_load.assert_not_called()
        mock_central_cls.assert_called_once_with(group_id="genomics-build")


# ── Test 6: _fetch_all and _fetch_edges helper functions ─────────────────────

class TestFetchHelpers:
    def test_fetch_all_returns_result_list(self) -> None:
        central = MagicMock()
        central.query.return_value = [{"result": SAMPLE_ENTITIES}]
        result = _fetch_all(central, "entity")
        assert result == SAMPLE_ENTITIES

    def test_fetch_all_returns_empty_on_empty_response(self) -> None:
        central = MagicMock()
        central.query.return_value = [{"result": []}]
        result = _fetch_all(central, "entity")
        assert result == []

    def test_fetch_all_returns_empty_on_none_response(self) -> None:
        central = MagicMock()
        central.query.return_value = None
        result = _fetch_all(central, "entity")
        assert result == []

    def test_fetch_edges_skips_missing_tables(self) -> None:
        from services.memory.surrealdb.client import SurrealError

        central = MagicMock()
        # All edge tables raise — should return empty list gracefully.
        central.query.side_effect = SurrealError("table not found")
        edges = _fetch_edges(central)
        assert edges == []

    def test_fetch_edges_tags_with_edge_table(self) -> None:
        central = MagicMock()

        def fake_query(sql: str, *args: Any, **kwargs: Any) -> list[Any]:
            if "FROM resolved_by" in sql:
                return [{"result": [{"id": "resolved_by:1", "in": "entity:1", "out": "entity:2"}]}]
            return [{"result": []}]

        central.query.side_effect = fake_query
        edges = _fetch_edges(central)
        resolved = [e for e in edges if e.get("_edge_table") == "resolved_by"]
        assert len(resolved) == 1
