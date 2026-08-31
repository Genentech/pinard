"""Unit tests for services/memory/surrealdb/schema_gen.py.

Tests:
- _to_snake converts CamelCase correctly
- generate_schema_ddl includes base tables, meta-tables, staging tables
- generate_schema_ddl includes domain edge tables (not just core)
- generate_schema_ddl does NOT include hardcoded core edge tables when none are in ontology
- populate_ontology_meta issues correct UPSERT queries
- read_ontology_version returns stored version
"""
from __future__ import annotations

import json
import sys
import os
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from services.memory.surrealdb.schema_gen import (
    _to_snake,
    generate_schema_ddl,
    populate_ontology_meta,
    read_ontology_version,
)
from services.memory.ontology.registry import OntologyRegistry
from services.memory.ontology.edges import CoreEdge, DependsOn
from services.memory.ontology.entities import CoreEntity
from pydantic import Field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_surreal() -> MagicMock:
    surreal = MagicMock()
    surreal.query = MagicMock(return_value=[[]])
    return surreal


class GWASStudy(CoreEntity):
    role: str = "gwas_study"
    study_id: str = Field("", description="GWAS study ID")


class RunsIn(CoreEdge):
    source_type: str = Field(..., description="runner")
    target_type: str = Field(..., description="environment")


# ---------------------------------------------------------------------------
# _to_snake
# ---------------------------------------------------------------------------

class TestToSnake:
    def test_depends_on(self):
        assert _to_snake("DependsOn") == "depends_on"

    def test_indicates_problem(self):
        assert _to_snake("IndicatesProblem") == "indicates_problem"

    def test_triggers_decision(self):
        assert _to_snake("TriggersDecision") == "triggers_decision"

    def test_requires_condition(self):
        assert _to_snake("RequiresCondition") == "requires_condition"

    def test_already_snake(self):
        assert _to_snake("depends_on") == "depends_on"

    def test_single_word(self):
        assert _to_snake("Produces") == "produces"

    def test_runs_in(self):
        assert _to_snake("RunsIn") == "runs_in"


# ---------------------------------------------------------------------------
# generate_schema_ddl
# ---------------------------------------------------------------------------

class TestGenerateSchemaDDL:
    def _compose_core(self) -> object:
        registry = OntologyRegistry()
        return registry.compose("pinard")

    def _compose_domain(self) -> object:
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="genomics-build",
            entity_types=[GWASStudy],
            edge_types=[RunsIn],
            domain_name="genomics",
            domain_version="1.0.0",
        )
        return registry.compose("genomics-build")

    def test_contains_entity_table(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL" in ddl

    def test_contains_wiki_doc_table(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE TABLE IF NOT EXISTS wiki_doc SCHEMAFULL" in ddl

    def test_wiki_doc_has_type_field(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE FIELD IF NOT EXISTS type         ON wiki_doc TYPE string" in ddl

    def test_wiki_doc_has_content_hash_field(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE FIELD IF NOT EXISTS content_hash ON wiki_doc TYPE string" in ddl

    def test_wiki_doc_path_unique_index(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE INDEX IF NOT EXISTS wiki_doc_path  ON wiki_doc FIELDS path UNIQUE" in ddl

    def test_wiki_doc_title_not_unique(self):
        ddl = generate_schema_ddl(self._compose_core())
        # title index must NOT be UNIQUE
        assert "DEFINE INDEX IF NOT EXISTS wiki_doc_title ON wiki_doc FIELDS title" in ddl
        # Ensure the title index line does not carry UNIQUE
        for line in ddl.splitlines():
            if "wiki_doc_title" in line and "DEFINE INDEX" in line:
                assert "UNIQUE" not in line

    def test_wiki_references_has_edge_type_field(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE FIELD IF NOT EXISTS edge_type  ON wiki_references TYPE string" in ddl

    def test_wiki_mentions_has_edge_type_field(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE FIELD IF NOT EXISTS edge_type  ON wiki_mentions TYPE string" in ddl

    def test_contains_ontology_meta_tables(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE TABLE IF NOT EXISTS ontology_entity_type SCHEMAFULL" in ddl
        assert "DEFINE TABLE IF NOT EXISTS ontology_edge_type SCHEMAFULL" in ddl
        assert "DEFINE TABLE IF NOT EXISTS ontology_version SCHEMAFULL" in ddl

    def test_contains_staging_tables(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "DEFINE TABLE IF NOT EXISTS entity_staging SCHEMAFULL" in ddl
        assert "DEFINE TABLE IF NOT EXISTS edge_staging SCHEMAFULL" in ddl

    def test_entity_staging_has_embedding_index(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "entity_staging_embedding_hnsw" in ddl

    def test_edge_staging_has_embedding_index(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "edge_staging_embedding_hnsw" in ddl

    def test_edge_staging_has_dedup_unique_index(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "edge_staging_dedup" in ddl
        assert "from_name, to_name, proposed_relation UNIQUE" in ddl

    def test_contains_core_edge_tables(self):
        ddl = generate_schema_ddl(self._compose_core())
        for edge_name in ["depends_on", "produces", "consumes",
                          "indicates_problem", "resolved_by",
                          "requires_condition", "triggers_decision"]:
            assert f"DEFINE TABLE IF NOT EXISTS {edge_name} SCHEMAFULL TYPE RELATION" in ddl, \
                f"Missing edge table: {edge_name}"

    def test_contains_domain_edge_table(self):
        ddl = generate_schema_ddl(self._compose_domain())
        assert "DEFINE TABLE IF NOT EXISTS runs_in SCHEMAFULL TYPE RELATION" in ddl

    def test_domain_ddl_also_contains_core_edges(self):
        ddl = generate_schema_ddl(self._compose_domain())
        assert "DEFINE TABLE IF NOT EXISTS depends_on SCHEMAFULL TYPE RELATION" in ddl

    def test_edge_tables_are_relation_type(self):
        ddl = generate_schema_ddl(self._compose_core())
        assert "TYPE RELATION IN entity OUT entity" in ddl

    def test_no_bare_use_ns(self):
        ddl = generate_schema_ddl(self._compose_core())
        bare_use_ns = [
            line.strip()
            for line in ddl.splitlines()
            if line.strip().upper().startswith("USE NS") and not line.strip().startswith("--")
        ]
        assert bare_use_ns == []


# ---------------------------------------------------------------------------
# populate_ontology_meta
# ---------------------------------------------------------------------------

class TestPopulateOntologyMeta:
    def test_upserts_entity_types(self):
        registry = OntologyRegistry()
        composed = registry.compose("pinard")
        surreal = _make_mock_surreal()
        populate_ontology_meta(surreal, composed)
        calls = surreal.query.call_args_list
        sqls = [c[0][0] for c in calls]
        entity_upserts = [s for s in sqls if "ontology_entity_type" in s]
        # One upsert per entity type
        assert len(entity_upserts) == len(composed.entity_types)

    def test_upserts_edge_types(self):
        registry = OntologyRegistry()
        composed = registry.compose("pinard")
        surreal = _make_mock_surreal()
        populate_ontology_meta(surreal, composed)
        calls = surreal.query.call_args_list
        sqls = [c[0][0] for c in calls]
        edge_upserts = [s for s in sqls if "ontology_edge_type" in s]
        assert len(edge_upserts) == len(composed.edge_types)

    def test_upserts_ontology_version(self):
        registry = OntologyRegistry()
        composed = registry.compose("pinard")
        surreal = _make_mock_surreal()
        populate_ontology_meta(surreal, composed)
        calls = surreal.query.call_args_list
        sqls = [c[0][0] for c in calls]
        version_upserts = [s for s in sqls if "ontology_version" in s]
        assert len(version_upserts) == 1

    def test_domain_entity_type_has_domain_name(self):
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="genomics-build",
            entity_types=[GWASStudy],
            domain_name="genomics",
            domain_version="1.0.0",
        )
        composed = registry.compose("genomics-build")
        surreal = _make_mock_surreal()
        populate_ontology_meta(surreal, composed)
        calls = surreal.query.call_args_list
        # Find the call for GWASStudy
        gwas_calls = [
            c for c in calls
            if "ontology_entity_type" in c[0][0]
            and c[0][1].get("name") == "GWASStudy"
        ]
        assert len(gwas_calls) == 1
        assert gwas_calls[0][0][1]["domain"] == "genomics"

    def test_version_stamp_includes_domain(self):
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="genomics-build",
            domain_name="genomics",
            domain_version="2.0.0",
        )
        composed = registry.compose("genomics-build")
        surreal = _make_mock_surreal()
        populate_ontology_meta(surreal, composed)
        calls = surreal.query.call_args_list
        version_call = [c for c in calls if "ontology_version" in c[0][0]][0]
        vars_ = version_call[0][1]
        assert vars_["domain_name"] == "genomics"
        assert vars_["domain_version"] == "2.0.0"

    def test_valid_pairs_json_serializable(self):
        registry = OntologyRegistry()
        composed = registry.compose("pinard")
        surreal = _make_mock_surreal()
        populate_ontology_meta(surreal, composed)
        calls = surreal.query.call_args_list
        edge_calls = [c for c in calls if "ontology_edge_type" in c[0][0]]
        for c in edge_calls:
            vars_ = c[0][1]
            # Must be valid JSON
            pairs = json.loads(vars_["pairs_json"])
            assert isinstance(pairs, list)


# ---------------------------------------------------------------------------
# read_ontology_version
# ---------------------------------------------------------------------------

class TestReadOntologyVersion:
    def test_returns_none_when_no_rows(self):
        surreal = _make_mock_surreal()
        surreal.query.return_value = [[]]
        result = read_ontology_version(surreal)
        assert result is None

    def test_returns_first_row(self):
        surreal = _make_mock_surreal()
        row = {"core_version": "1.0.0", "domain_name": "genomics"}
        surreal.query.return_value = [[row]]
        result = read_ontology_version(surreal)
        assert result == row

    def test_handles_flat_dict_result(self):
        surreal = _make_mock_surreal()
        row = {"core_version": "1.0.0"}
        surreal.query.return_value = [row]
        result = read_ontology_version(surreal)
        # When result[0] is a dict (not a list), returns it directly
        assert result == row
