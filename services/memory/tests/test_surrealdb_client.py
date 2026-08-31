"""Unit tests for services/memory/surrealdb/client.py (SDK-based transport).

All tests use mocks — no running SurrealDB instance is required.

Run from repo root:
    pytest services/memory/tests/test_surrealdb_client.py

Test coverage:
- Colon-string round-trip integrity (bug #5 regression guard)
- Idempotent re-ingest via deterministic id (bug #4)
- NS/DB context set per group_id including hyphenated names (bug #3)
- Per-statement error surfacing (bug #2)
- Schema applied without bare USE NS (bug #1)
- KNN recall uses integer literals for K/EF, not bound params (SurrealDB 3.2.0)
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Ensure the repo root (three levels up from this file: tests/ -> memory/ ->
# services/ -> repo-root/) is on sys.path so `services.memory.*` imports work
# regardless of where pytest is invoked from.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import services.memory.surrealdb.client as _client_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_db() -> MagicMock:
    """Return a mock that behaves like BlockingHttpSurrealConnection."""
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
    """Construct a SurrealClient with a patched Surreal constructor."""
    _client_mod._schema_applied.discard(group_id)
    if mock_db is None:
        mock_db = _make_mock_db()
    with patch("services.memory.surrealdb.client.Surreal", return_value=mock_db):
        from services.memory.surrealdb.client import SurrealClient
        client = SurrealClient(group_id=group_id, url="http://localhost:8000",
                               user="root", password="secret")
    return client, mock_db


# ---------------------------------------------------------------------------
# Test 1: NS/DB context
# ---------------------------------------------------------------------------

class TestNsDbContext:
    def test_use_called_with_namespace_and_group_id(self):
        client, db = _make_client(group_id="exo-cli")
        db.use.assert_called_once_with("pinard", "exo-cli")

    def test_hyphenated_group_id(self):
        """Hyphenated group_id (like 'vignoble-exohub') must be passed verbatim."""
        client, db = _make_client(group_id="vignoble-exohub")
        db.use.assert_called_once_with("pinard", "vignoble-exohub")

    def test_signin_called_with_credentials(self):
        client, db = _make_client()
        db.signin.assert_called_once_with({"username": "root", "password": "secret"})


# ---------------------------------------------------------------------------
# Test 2: Per-statement error surfacing
# ---------------------------------------------------------------------------

class TestPerStatementErrorSurfacing:
    def test_query_raises_on_err_statement(self):
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "ERR", "result": "Specify a database to use"}]
        }
        from services.memory.surrealdb.client import SurrealError
        with pytest.raises(SurrealError, match="Specify a database"):
            client.query("SELECT * FROM entity")

    def test_query_raises_on_second_err_statement(self):
        """ERR in any statement (not just the first) must surface."""
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [
                {"status": "OK", "result": []},
                {"status": "ERR", "result": "Table not found"},
            ]
        }
        from services.memory.surrealdb.client import SurrealError
        with pytest.raises(SurrealError, match="Table not found"):
            client.query("SELECT 1; SELECT * FROM missing_table")

    def test_query_ok_returns_results(self):
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [{"id": "entity:abc", "name": "foo"}]}]
        }
        result = client.query("SELECT * FROM entity")
        assert result == [[{"id": "entity:abc", "name": "foo"}]]

    def test_apply_schema_raises_on_err_statement(self):
        """Schema application must fail loudly on any ERR statement."""
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [
                {"status": "OK", "result": None},
                {"status": "ERR", "result": "Specify a database to use"},
            ]
        }
        with tempfile.NamedTemporaryFile(suffix=".surql", mode="w", delete=False) as f:
            f.write("DEFINE TABLE entity SCHEMAFULL; DEFINE FIELD name ON entity TYPE string;")
            fpath = f.name
        try:
            from services.memory.surrealdb.client import SurrealError
            with pytest.raises(SurrealError, match="Specify a database"):
                client.apply_schema(fpath)
        finally:
            os.unlink(fpath)


# ---------------------------------------------------------------------------
# Test 3: Schema — no bare USE NS in schema.surql
# ---------------------------------------------------------------------------

class TestSchemaFile:
    def test_schema_surql_has_no_bare_use_ns(self):
        """schema.surql must not contain `USE NS pinard;` — that wipes DB context."""
        schema_path = pathlib.Path(__file__).parent.parent / "surrealdb" / "schema.surql"
        text = schema_path.read_text()
        bare_use_ns_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().upper().startswith("USE NS") and not line.strip().startswith("--")
        ]
        assert bare_use_ns_lines == [], (
            f"schema.surql contains bare USE NS statement(s): {bare_use_ns_lines}"
        )


# ---------------------------------------------------------------------------
# Test 4: Idempotent upsert via deterministic record id
# ---------------------------------------------------------------------------

class TestIdempotentUpsert:
    def _entity_rid(self, role: str, name: str) -> str:
        return hashlib.sha256(f"{role}\x00{name}".encode()).hexdigest()[:32]

    def test_upsert_entity_uses_deterministic_rid(self):
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [{"id": "entity:abc", "name": "exo-cli"}]}]
        }
        client.upsert_entity(role="project", name="exo-cli", description="CLI tool")
        call_args = db.query_raw.call_args
        sql = call_args[0][0]
        vars_ = call_args[0][1]
        assert "type::record('entity', $rid)" in sql
        expected_rid = self._entity_rid("project", "exo-cli")
        assert vars_["rid"] == expected_rid

    def test_upsert_entity_same_rid_on_repeated_calls(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_entity(role="project", name="exo-cli")
        client.upsert_entity(role="project", name="exo-cli")
        calls = db.query_raw.call_args_list
        assert calls[0][0][1]["rid"] == calls[1][0][1]["rid"]

    def test_upsert_entity_different_rid_for_different_entities(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_entity(role="project", name="exo-cli")
        client.upsert_entity(role="project", name="pinard")
        calls = db.query_raw.call_args_list
        assert calls[0][0][1]["rid"] != calls[1][0][1]["rid"]


# ---------------------------------------------------------------------------
# Test 5: Colon-string round-trip integrity (bug #5 regression guard)
# ---------------------------------------------------------------------------

class TestColonStringIntegrity:
    COLON_STRINGS = [
        "What: exo-cli's main-branch release is driven by commitizen",
        "Why: reduce manual overhead",
        "Note: exo doctor checks path entries",
        "key: value with spaces",
    ]

    def test_colon_strings_passed_as_vars_not_inline(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        for s in self.COLON_STRINGS:
            db.query_raw.reset_mock()
            client.upsert_entity(role="observation", name="obs-1", description=s)
            call_args = db.query_raw.call_args
            sql = call_args[0][0]
            vars_ = call_args[0][1]
            assert s not in sql, f"Colon string inlined into SQL: {s!r}"
            assert vars_.get("description") == s, f"Colon string not in vars: {vars_.get('description')!r}"

    def test_upsert_entity_description_var_key(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        desc = "What: release automation commitizen version bump"
        client.upsert_entity(role="observation", name="obs-15", description=desc)
        vars_ = db.query_raw.call_args[0][1]
        assert vars_["description"] == desc


# ---------------------------------------------------------------------------
# Test 6: KNN recall uses integer literals for K/EF (SurrealDB 3.2.0)
# ---------------------------------------------------------------------------

class TestKnnRecallLiterals:
    """SurrealDB 3.2.0 rejects bound parameters for K/EF in the KNN operator.

    Regression guard: recall() must inline K and EF as integer literals in the
    SQL string, while $vec remains a bound parameter.
    """

    def test_recall_inlines_k_and_ef_as_literals(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        vec = [0.1] * 1024
        client.recall(embedding=vec, limit=5, ef=50)
        sql = db.query_raw.call_args[0][0]
        # K=5 and EF=50 must appear as literals in the operator
        assert "<|5,50|>" in sql, f"KNN operator not inlined: {sql}"
        # $vec must remain a bound param, not inlined
        assert "$vec" in sql

    def test_recall_with_role_filter_inlines_literals(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        vec = [0.2] * 1024
        client.recall(embedding=vec, limit=10, ef=100, role_filter="observation")
        sql = db.query_raw.call_args[0][0]
        assert "<|10,100|>" in sql, f"KNN operator not inlined: {sql}"
        assert "$vec" in sql
        assert "$role" in sql

    def test_recall_does_not_pass_k_ef_as_vars(self):
        """k and ef must NOT be in the vars dict (they are inlined)."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall(embedding=[0.0] * 1024, limit=7, ef=80)
        vars_ = db.query_raw.call_args[0][1]
        assert "k" not in vars_, "k should not be a bound param"
        assert "ef" not in vars_, "ef should not be a bound param"
        assert "vec" in vars_

    def test_recall_default_params(self):
        """Default limit=10, ef=100 must appear as literals."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall(embedding=[0.0] * 1024)
        sql = db.query_raw.call_args[0][0]
        assert "<|10,100|>" in sql


# Test 7: relate() uses type::record() refs with sha256 IDs (SurrealDB 3.2.0)
# ---------------------------------------------------------------------------

class TestRelate:
    """Regression guard for the invalid (SELECT…)[0].id RELATE syntax.

    SurrealDB 3.2.0 rejects subquery-indexing in RELATE arrow positions.
    relate() must use type::record() refs with the same deterministic sha256
    IDs that upsert_entity() uses.
    """

    def _rid(self, role: str, name: str) -> str:
        return hashlib.sha256(f"{role}\x00{name}".encode()).hexdigest()[:32]

    def test_relate_uses_type_record_refs(self):
        """SQL must use type::record() for both endpoints, not SELECT subqueries."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.relate("step", "ingest-vcf", "produces", "artifact", "normalized parquet")
        sql = db.query_raw.call_args[0][0]
        assert "type::record('entity', $from_rid)" in sql
        assert "type::record('entity', $to_rid)" in sql

    def test_relate_does_not_use_select_subquery(self):
        """The old (SELECT id FROM entity …)[0].id form must not appear."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.relate("step", "ingest-vcf", "produces", "artifact", "normalized parquet")
        sql = db.query_raw.call_args[0][0]
        assert "SELECT" not in sql
        assert "[0]" not in sql

    def test_relate_sha256_ids_match_upsert_entity(self):
        """from_rid and to_rid must match the sha256 IDs upsert_entity() uses."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.relate("step", "ingest-vcf", "produces", "artifact", "normalized parquet")
        vars_ = db.query_raw.call_args[0][1]
        assert vars_["from_rid"] == self._rid("step", "ingest-vcf")
        assert vars_["to_rid"] == self._rid("artifact", "normalized parquet")

    def test_relate_relation_identifier_sanitized(self):
        """Non-alnum/_ characters in relation name must be stripped."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.relate("a", "x", "dep.ends-on!", "b", "y")
        sql = db.query_raw.call_args[0][0]
        # Only alnum and _ survive — dots, dashes, exclamation stripped
        assert "dependson" in sql or "dep_ends_on" in sql or "dependson" in sql
        # The sanitized form must appear between the arrows
        import re
        m = re.search(r"->([^-]+)->", sql)
        assert m is not None
        sanitized = m.group(1)
        assert all(ch.isalnum() or ch == "_" for ch in sanitized)

    def test_relate_description_cast_with_string(self):
        """description must be cast with <string> in the SET clause."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.relate("step", "qc-filter", "produces", "artifact", "QC report",
                      description="What: QC filter produces a quality report")
        sql = db.query_raw.call_args[0][0]
        assert "<string>$description" in sql


# ---------------------------------------------------------------------------
# Test 8: ensure_schema with registry (dynamic schema generation)
# ---------------------------------------------------------------------------

class TestEnsureSchemaWithRegistry:
    """ensure_schema(registry=..., group_id=...) must generate DDL dynamically."""

    def test_ensure_schema_with_registry_calls_apply_schema(self):
        """When registry+group_id are provided, DDL is generated and applied."""
        from services.memory.ontology.registry import OntologyRegistry
        client, db = _make_client(group_id="genomics-build")
        # Reset the schema_applied cache so it re-applies.
        import services.memory.surrealdb.client as _cm
        _cm._schema_applied.discard("genomics-build")
        # Also discard any tuple cache keys.
        to_remove = [k for k in _cm._schema_applied if isinstance(k, tuple) and k[0] == "genomics-build"]
        for k in to_remove:
            _cm._schema_applied.discard(k)

        registry = OntologyRegistry()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        # Should not raise and should call query_raw (for schema + meta population).
        with patch("services.memory.surrealdb.client.Surreal", return_value=db):
            client.ensure_schema(registry=registry, group_id="genomics-build")
        # apply_schema invokes query_raw at least once.
        assert db.query_raw.called

    def test_ensure_schema_without_registry_uses_static_file(self):
        """Without registry, static schema path is applied."""
        client, db = _make_client(group_id="fallback-group")
        import services.memory.surrealdb.client as _cm
        _cm._schema_applied.discard("fallback-group")

        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        import tempfile, pathlib
        with tempfile.NamedTemporaryFile(suffix=".surql", mode="w", delete=False) as f:
            f.write("DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;")
            fpath = f.name
        try:
            client.ensure_schema(schema_path=fpath)
            assert db.query_raw.called
        finally:
            import os as _os
            _os.unlink(fpath)

    def test_ensure_schema_idempotent_with_registry(self):
        """Second call with same version should not re-apply."""
        from services.memory.ontology.registry import OntologyRegistry
        client, db = _make_client(group_id="idempotent-group")
        import services.memory.surrealdb.client as _cm
        # Discard any prior cache for this group.
        to_remove = [k for k in _cm._schema_applied if isinstance(k, tuple) and k[0] == "idempotent-group"]
        for k in to_remove:
            _cm._schema_applied.discard(k)
        _cm._schema_applied.discard("idempotent-group")

        registry = OntologyRegistry()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        with patch("services.memory.surrealdb.client.Surreal", return_value=db):
            client.ensure_schema(registry=registry, group_id="idempotent-group")
            first_call_count = db.query_raw.call_count
            client.ensure_schema(registry=registry, group_id="idempotent-group")
            second_call_count = db.query_raw.call_count
        # Second call should not have issued more queries.
        assert second_call_count == first_call_count


# ---------------------------------------------------------------------------
# Test 9: Staging upserts
# ---------------------------------------------------------------------------

class TestStagingUpserts:
    """upsert_entity_staging and upsert_edge_staging route to staging tables."""

    def test_upsert_entity_staging_uses_entity_staging_table(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_entity_staging(
            name="novel-concept",
            proposed_role="unknown_role",
            description="Something new",
            rationale="not in ontology",
            provenance="episode_extraction",
        )
        sql = db.query_raw.call_args[0][0]
        assert "entity_staging" in sql
        assert "type::record('entity_staging'" in sql

    def test_upsert_entity_staging_deterministic_rid(self):
        import hashlib
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_entity_staging(name="foo-concept")
        vars_ = db.query_raw.call_args[0][1]
        expected_rid = hashlib.sha256(f"staging\x00foo-concept".encode()).hexdigest()[:32]
        assert vars_["rid"] == expected_rid

    def test_upsert_entity_staging_with_embedding(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        vec = [0.1] * 1024
        client.upsert_entity_staging(name="staged", embedding=vec)
        sql = db.query_raw.call_args[0][0]
        vars_ = db.query_raw.call_args[0][1]
        assert "$embedding" in sql
        assert vars_["embedding"] == vec

    def test_upsert_edge_staging_uses_edge_staging_table(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_edge_staging(
            from_name="slurm-job-1",
            from_role="task",
            to_name="hpc-cluster",
            to_role="environment_condition",
            proposed_relation="scheduled_on",
            description="Slurm job runs on HPC",
            rationale="not in ontology",
            provenance="episode_extraction",
        )
        sql = db.query_raw.call_args[0][0]
        assert "edge_staging" in sql
        assert "type::record('edge_staging'" in sql

    def test_upsert_edge_staging_description_cast(self):
        """description must be cast with <string> to avoid SurrealDB colon parsing."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_edge_staging(
            from_name="a", from_role="task",
            to_name="b", to_role="artifact",
            proposed_relation="custom_rel",
            description="What: a novel edge",
        )
        sql = db.query_raw.call_args[0][0]
        assert "<string>$description" in sql

    def test_upsert_edge_staging_deterministic_rid(self):
        import hashlib
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_edge_staging(
            from_name="src", from_role="task",
            to_name="tgt", to_role="artifact",
            proposed_relation="my_rel",
        )
        vars_ = db.query_raw.call_args[0][1]
        key = "staging_edge\x00src\x00my_rel\x00tgt"
        expected_rid = hashlib.sha256(key.encode()).hexdigest()[:32]
        assert vars_["rid"] == expected_rid

    def test_upsert_edge_staging_same_rid_repeated(self):
        import hashlib
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.upsert_edge_staging(from_name="x", from_role="r", to_name="y", to_role="r2", proposed_relation="z")
        client.upsert_edge_staging(from_name="x", from_role="r", to_name="y", to_role="r2", proposed_relation="z")
        calls = db.query_raw.call_args_list
        assert calls[0][0][1]["rid"] == calls[1][0][1]["rid"]


# ---------------------------------------------------------------------------
# Test: recall_wiki and lookup_wiki
# ---------------------------------------------------------------------------

class TestRecallWiki:
    """Tests for the new recall_wiki() and lookup_wiki() methods."""

    def test_recall_wiki_uses_integer_literals_for_k_ef(self):
        """recall_wiki must embed K and EF as integer literals in the SQL string."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall_wiki([0.1] * 1024, limit=5, ef=50)
        sql = db.query_raw.call_args[0][0]
        assert "<|5,50|>" in sql, f"Expected <|5,50|> in SQL, got: {sql}"

    def test_recall_wiki_default_filters_auto_serve_only(self):
        """By default (include_needs_review=False) SQL must contain status = 'auto_serve'."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall_wiki([0.0] * 1024)
        sql = db.query_raw.call_args[0][0]
        assert "auto_serve" in sql
        assert "needs_review" not in sql

    def test_recall_wiki_include_needs_review_removes_status_filter(self):
        """With include_needs_review=True status filter must be absent."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall_wiki([0.0] * 1024, include_needs_review=True)
        sql = db.query_raw.call_args[0][0]
        # No status restriction — both statuses are served.
        assert "auto_serve" not in sql

    def test_recall_wiki_selects_correct_fields(self):
        """recall_wiki must select path, title, type, body, confidence, status, dist."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall_wiki([0.0] * 1024)
        sql = db.query_raw.call_args[0][0]
        for field in ("path", "title", "type", "body", "confidence", "status", "dist"):
            assert field in sql, f"Field {field!r} missing from recall_wiki SQL"

    def test_recall_wiki_returns_list(self):
        """recall_wiki returns a list of dicts."""
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [
                {"path": "concepts/oom", "title": "OOM", "type": "concept",
                 "body": "body text", "confidence": 0.9, "status": "auto_serve", "dist": 0.1}
            ]}]
        }
        results = client.recall_wiki([0.0] * 1024)
        assert isinstance(results, list)
        assert results[0]["path"] == "concepts/oom"
        assert results[0]["dist"] == 0.1

    def test_recall_wiki_returns_empty_on_no_results(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        assert client.recall_wiki([0.0] * 1024) == []

    def test_lookup_wiki_searches_title_and_body(self):
        """lookup_wiki SQL must reference both title and body FTS indexes."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.lookup_wiki("memory leak")
        sql = db.query_raw.call_args[0][0]
        assert "title" in sql
        assert "body" in sql

    def test_lookup_wiki_default_filters_auto_serve(self):
        """By default lookup_wiki must restrict to auto_serve."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.lookup_wiki("oom")
        sql = db.query_raw.call_args[0][0]
        assert "auto_serve" in sql

    def test_lookup_wiki_include_needs_review(self):
        """With include_needs_review=True the status filter must be absent."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.lookup_wiki("oom", include_needs_review=True)
        sql = db.query_raw.call_args[0][0]
        assert "auto_serve" not in sql

    def test_lookup_wiki_passes_text_and_limit_as_bound_params(self):
        """Text and limit must be passed as bound parameters, not interpolated."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.lookup_wiki("leak", limit=7)
        vars_ = db.query_raw.call_args[0][1]
        assert vars_["text"] == "leak"
        assert vars_["limit"] == 7

    def test_lookup_wiki_returns_list(self):
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [
                {"path": "actions/fix-oom", "title": "Fix OOM", "type": "action",
                 "body": "patch body", "confidence": 0.95, "status": "auto_serve", "score": 0.88}
            ]}]
        }
        results = client.lookup_wiki("oom fix")
        assert isinstance(results, list)
        assert results[0]["path"] == "actions/fix-oom"


class TestWikiChunkMethods:
    """Tests for delete_wiki_chunks_by_path, upsert_wiki_chunks, recall_wiki_chunks."""

    def test_delete_wiki_chunks_by_path_uses_parent_path_param(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.delete_wiki_chunks_by_path("docs/setup")
        sql, vars_ = db.query_raw.call_args[0]
        assert "wiki_chunk" in sql
        assert "parent_path" in sql
        assert vars_["path"] == "docs/setup"

    def test_delete_wiki_chunks_by_path_returns_count(self):
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [{"id": "wiki_chunk:abc"}, {"id": "wiki_chunk:def"}]}]
        }
        count = client.delete_wiki_chunks_by_path("docs/setup")
        assert count == 2

    def test_upsert_wiki_chunks_uses_deterministic_id(self):
        import hashlib as _hashlib
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        chunks = [{"parent_path": "docs/setup", "heading": "Install", "chunk_index": 0, "text": "Run pip install."}]
        client.upsert_wiki_chunks(chunks)
        sql, vars_ = db.query_raw.call_args[0]
        expected_rid = _hashlib.sha256(b"wiki_chunk\x00docs/setup\x000").hexdigest()[:32]
        assert vars_["rid"] == expected_rid
        assert vars_["parent_path"] == "docs/setup"
        assert vars_["heading"] == "Install"
        assert vars_["chunk_index"] == 0

    def test_upsert_wiki_chunks_includes_embedding_when_provided(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        emb = [0.1] * 1024
        chunks = [{"parent_path": "p", "heading": "H", "chunk_index": 0, "text": "t", "embedding": emb}]
        client.upsert_wiki_chunks(chunks)
        sql, vars_ = db.query_raw.call_args[0]
        assert "embedding" in sql
        assert vars_["embedding"] == emb

    def test_upsert_wiki_chunks_omits_embedding_when_absent(self):
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        chunks = [{"parent_path": "p", "heading": "H", "chunk_index": 0, "text": "t"}]
        client.upsert_wiki_chunks(chunks)
        sql, vars_ = db.query_raw.call_args[0]
        assert "embedding" not in sql
        assert "embedding" not in vars_

    def test_recall_wiki_chunks_uses_hnsw_knn_literals(self):
        """K and EF must be integer literals in the KNN operator, not bound params."""
        client, db = _make_client()
        db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
        client.recall_wiki_chunks([0.0] * 1024, limit=5, ef=50)
        sql = db.query_raw.call_args[0][0]
        assert "<|5,50|>" in sql
        assert "wiki_chunk" in sql
        assert "parent_path" in sql
        assert "dist" in sql

    def test_recall_wiki_chunks_returns_list(self):
        client, db = _make_client()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [
                {"parent_path": "docs/hpc", "heading": "Singularity", "chunk_index": 1,
                 "text": "Use singularity exec...", "dist": 0.15}
            ]}]
        }
        results = client.recall_wiki_chunks([0.0] * 1024)
        assert isinstance(results, list)
        assert results[0]["parent_path"] == "docs/hpc"
        assert results[0]["dist"] == 0.15


# ---------------------------------------------------------------------------
# Test: SCHEMALESS migration (bug #191 regression guard)
# ---------------------------------------------------------------------------

class TestMigrateSchemalessTables:
    """_migrate_schemaless_tables() must detect and fix SCHEMALESS base tables.

    Regression guard for: ensure_schema fails on pre-existing SCHEMALESS tables
    (FLEXIBLE error) — vignoble-targetnexus scope drift (issue #191).
    """

    def _make_info_response(self, tables: dict) -> dict:
        """Build a fake INFO FOR DB response with the given tables mapping."""
        return {
            "result": [{"status": "OK", "result": {"tables": tables}}]
        }

    def test_schemaless_entity_table_triggers_overwrite(self):
        """When entity exists as SCHEMALESS, DEFINE TABLE OVERWRITE … SCHEMAFULL is issued."""
        client, db = _make_client(group_id="vignoble-targetnexus")
        # First call: INFO FOR DB returns entity as SCHEMALESS.
        # Subsequent calls: return OK (for DEFINE TABLE OVERWRITE + any schema DDL).
        info_resp = self._make_info_response({
            "entity": "DEFINE TABLE entity SCHEMALESS PERMISSIONS NONE",
        })
        ok_resp = {"result": [{"status": "OK", "result": None}]}
        db.query_raw.side_effect = [info_resp, ok_resp]

        client._migrate_schemaless_tables()

        calls = [c[0][0] for c in db.query_raw.call_args_list]
        assert calls[0] == "INFO FOR DB"
        assert any("DEFINE TABLE OVERWRITE entity SCHEMAFULL" in c for c in calls), (
            f"Expected DEFINE TABLE OVERWRITE entity SCHEMAFULL in calls: {calls}"
        )

    def test_schemafull_entity_table_is_not_overwritten(self):
        """A table already SCHEMAFULL must not trigger an OVERWRITE."""
        client, db = _make_client(group_id="healthy-scope")
        info_resp = self._make_info_response({
            "entity": "DEFINE TABLE entity SCHEMAFULL PERMISSIONS NONE",
        })
        db.query_raw.return_value = info_resp

        client._migrate_schemaless_tables()

        calls = [c[0][0] for c in db.query_raw.call_args_list]
        assert all("OVERWRITE" not in c for c in calls), (
            f"Unexpected OVERWRITE call for SCHEMAFULL table: {calls}"
        )

    def test_unknown_table_is_not_overwritten(self):
        """Tables not in _SCHEMAFULL_TABLES must never be touched even if SCHEMALESS."""
        client, db = _make_client(group_id="custom-scope")
        info_resp = self._make_info_response({
            "my_custom_table": "DEFINE TABLE my_custom_table SCHEMALESS PERMISSIONS NONE",
        })
        db.query_raw.return_value = info_resp

        client._migrate_schemaless_tables()

        calls = [c[0][0] for c in db.query_raw.call_args_list]
        assert all("OVERWRITE" not in c for c in calls), (
            f"Unexpected OVERWRITE call for non-base table: {calls}"
        )

    def test_empty_database_is_noop(self):
        """INFO FOR DB with no tables must not produce any OVERWRITE calls."""
        client, db = _make_client(group_id="fresh-scope")
        info_resp = self._make_info_response({})
        db.query_raw.return_value = info_resp

        client._migrate_schemaless_tables()

        calls = [c[0][0] for c in db.query_raw.call_args_list]
        assert all("OVERWRITE" not in c for c in calls)

    def test_info_for_db_failure_is_silent(self):
        """If INFO FOR DB raises, _migrate_schemaless_tables must not propagate."""
        client, db = _make_client(group_id="broken-scope")
        db.query_raw.side_effect = RuntimeError("connection refused")

        # Must not raise.
        client._migrate_schemaless_tables()

    def test_ensure_schema_calls_migrate_before_apply(self):
        """ensure_schema() must call _migrate_schemaless_tables before apply_schema."""
        import services.memory.surrealdb.client as _cm
        client, db = _make_client(group_id="migrate-order-check")
        _cm._schema_applied.discard("migrate-order-check")

        call_order: list[str] = []

        original_migrate = client._migrate_schemaless_tables
        original_apply = client.apply_schema

        def _tracked_migrate():
            call_order.append("migrate")
            # Return immediately (no-op body — no INFO FOR DB call needed).
            db.query_raw.return_value = {"result": [{"status": "OK", "result": {"tables": {}}}]}
            original_migrate()

        def _tracked_apply(path: str):
            call_order.append("apply")
            db.query_raw.return_value = {"result": [{"status": "OK", "result": []}]}
            original_apply(path)

        client._migrate_schemaless_tables = _tracked_migrate  # type: ignore[method-assign]
        client.apply_schema = _tracked_apply  # type: ignore[method-assign]

        import tempfile, os as _os
        with tempfile.NamedTemporaryFile(suffix=".surql", mode="w", delete=False) as f:
            f.write("DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;")
            fpath = f.name
        try:
            client.ensure_schema(schema_path=fpath)
        finally:
            _os.unlink(fpath)

        assert call_order.index("migrate") < call_order.index("apply"), (
            f"migrate must run before apply; got order: {call_order}"
        )

    def test_wiki_doc_schemaless_triggers_overwrite(self):
        """wiki_doc SCHEMALESS must also be migrated (it has a FLEXIBLE frontmatter field)."""
        client, db = _make_client(group_id="wiki-scope")
        info_resp = self._make_info_response({
            "wiki_doc": "DEFINE TABLE wiki_doc SCHEMALESS PERMISSIONS NONE",
        })
        ok_resp = {"result": [{"status": "OK", "result": None}]}
        db.query_raw.side_effect = [info_resp, ok_resp]

        client._migrate_schemaless_tables()

        calls = [c[0][0] for c in db.query_raw.call_args_list]
        assert any("DEFINE TABLE OVERWRITE wiki_doc SCHEMAFULL" in c for c in calls)


# ---------------------------------------------------------------------------
# Test: manual_edit field in schema
# ---------------------------------------------------------------------------

class TestManualEditSchema:
    def test_schema_surql_defines_manual_edit_field(self):
        """schema.surql must define the manual_edit field on entity."""
        from services.memory.surrealdb.client import SCHEMA_PATH
        schema = SCHEMA_PATH.read_text()
        assert "manual_edit" in schema
        assert "TYPE bool" in schema

    def test_schema_gen_base_ddl_defines_manual_edit_field(self):
        """schema_gen._BASE_DDL must include the manual_edit field."""
        from services.memory.surrealdb.schema_gen import _BASE_DDL
        assert "manual_edit" in _BASE_DDL
        assert "TYPE bool" in _BASE_DDL


# ---------------------------------------------------------------------------
# Test: update_entity_description
# ---------------------------------------------------------------------------

class TestUpdateEntityDescription:
    def _make_update_client(self):
        from services.memory.surrealdb.client import SurrealClient
        from unittest.mock import patch, MagicMock
        _client_mod._schema_applied.discard("test-group")
        db = _make_mock_db()
        db.query_raw.return_value = {
            "result": [{"status": "OK", "result": [
                {"id": "entity:abc123", "description": "new text", "manual_edit": True}
            ]}]
        }
        with patch("services.memory.surrealdb.client.Surreal", return_value=db):
            client = SurrealClient(group_id="test-group", url="http://localhost:8000",
                                   user="root", password="secret")
        return client, db

    def test_update_entity_description_issues_update_query(self):
        client, db = self._make_update_client()
        result = client.update_entity_description("entity:abc123", "new text", [0.1] * 1024)
        assert db.query_raw.called
        raw_call = db.query_raw.call_args[0][0]
        assert "UPDATE" in raw_call
        assert "manual_edit = true" in raw_call
        assert "description" in raw_call

    def test_update_entity_description_does_not_overwrite_role(self):
        client, db = self._make_update_client()
        raw_call_sql = None

        def capture_query(sql, *args, **kwargs):
            nonlocal raw_call_sql
            raw_call_sql = sql
            return {"result": [{"status": "OK", "result": [{"id": "entity:abc123"}]}]}

        db.query_raw.side_effect = capture_query
        client.update_entity_description("entity:abc123", "updated description", None)
        assert raw_call_sql is not None
        # Must not set role, name, provenance
        assert "role =" not in raw_call_sql
        assert "name =" not in raw_call_sql
        assert "provenance =" not in raw_call_sql

    def test_update_entity_description_returns_empty_on_surreal_error(self):
        from services.memory.surrealdb.client import SurrealError
        client, db = self._make_update_client()
        db.query_raw.side_effect = Exception("connection refused")
        db.check_response_for_error.side_effect = None
        # Patch query to raise SurrealError
        with patch.object(client, "query", side_effect=SurrealError("fail")):
            result = client.update_entity_description("entity:abc123", "text", None)
        assert result == {}


# ---------------------------------------------------------------------------
# Test: upsert_entity clobber guard
# ---------------------------------------------------------------------------

class TestUpsertEntityClobberGuard:
    def _make_upsert_client(self, query_result=None):
        from services.memory.surrealdb.client import SurrealClient
        _client_mod._schema_applied.discard("guard-group")
        db = _make_mock_db()
        if query_result is not None:
            db.query_raw.return_value = {
                "result": [{"status": "OK", "result": query_result}]
            }
        else:
            db.query_raw.return_value = {
                "result": [{"status": "OK", "result": [{"id": "entity:abc"}]}]
            }
        with patch("services.memory.surrealdb.client.Surreal", return_value=db):
            client = SurrealClient(group_id="guard-group", url="http://localhost:8000",
                                   user="root", password="secret")
        return client, db

    def test_upsert_entity_sql_contains_manual_edit_conditional(self):
        """upsert_entity SQL must use IF manual_edit = true to guard description."""
        client, db = self._make_upsert_client()
        captured_sql = []

        def capture(sql, *args, **kwargs):
            captured_sql.append(sql)
            return {"result": [{"status": "OK", "result": [{"id": "entity:x"}]}]}

        db.query_raw.side_effect = capture
        client.upsert_entity(role="artifact", name="test", description="orig", embedding=[0.1] * 1024)
        assert any("manual_edit" in s for s in captured_sql), (
            "upsert_entity SQL must contain manual_edit conditional guard"
        )
        # Guard: description and embedding must be conditional
        upsert_sql = next(s for s in captured_sql if "UPSERT" in s)
        assert "IF manual_edit" in upsert_sql

    def test_upsert_entity_always_updates_provenance_and_data(self):
        """Non-guarded fields (role, name, provenance, data) are always SET."""
        client, db = self._make_upsert_client()
        captured_sql = []

        def capture(sql, *args, **kwargs):
            captured_sql.append(sql)
            return {"result": [{"status": "OK", "result": [{"id": "entity:x"}]}]}

        db.query_raw.side_effect = capture
        client.upsert_entity(role="artifact", name="test", description="d", provenance="ep")
        upsert_sql = next(s for s in captured_sql if "UPSERT" in s)
        # These must be unconditionally set
        assert "provenance = $provenance" in upsert_sql
        assert "data = $data" in upsert_sql
