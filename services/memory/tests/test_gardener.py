"""Unit tests for services/memory/ontology/gardener.py.

All tests use mocks — no running SurrealDB instance or LLM required.

Run from repo root:
    pytest services/memory/tests/test_gardener.py

Live smoke tests (require a real SurrealDB instance):
    pytest -m live services/memory/tests/test_gardener.py
    Env: SURREAL_URL, SURREAL_USER, SURREAL_PASS
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from services.memory.ontology.gardener import (
    GardenerDecision,
    OntologyGardenerConfig,
    StagingCluster,
    _cosine,
    _to_camel,
    _to_snake,
    apply_map_decision,
    cluster_proposals,
    decide_cluster,
    emit_proposal_mr,
    read_staging,
    run_gardener,
)
from services.memory.ontology.registry import OntologyRegistry


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_registry() -> OntologyRegistry:
    return OntologyRegistry()


def _make_composed(registry: OntologyRegistry | None = None):
    if registry is None:
        registry = _make_registry()
    return registry.compose("test-group")


def _make_surreal_mock(entity_rows=None, edge_rows=None):
    """Return a mock SurrealClient with configurable staging data."""
    m = MagicMock()
    m.list_entity_staging.return_value = entity_rows or []
    m.list_edge_staging.return_value = edge_rows or []
    m.upsert_entity.return_value = {}
    m.relate.return_value = {}
    return m


def _make_llm_mock(response_json: dict) -> MagicMock:
    """Return a mock LLMClient whose complete() returns a JSON string."""
    m = MagicMock()
    m.complete.return_value = json.dumps(response_json)
    return m


def _vec(n: int, dim: int = 4) -> list[float]:
    """Return a simple unit vector with 1.0 at position n % dim."""
    v = [0.0] * dim
    v[n % dim] = 1.0
    return v


# ── Tests: read_staging ────────────────────────────────────────────────────────


class TestReadStaging:
    def test_delegates_to_surreal(self):
        entity_rows = [{"name": "e1", "proposed_role": "compute_job", "occurrence_count": 5}]
        edge_rows = [{"from_name": "a", "proposed_relation": "submits_to", "occurrence_count": 3}]
        surreal = _make_surreal_mock(entity_rows=entity_rows, edge_rows=edge_rows)

        e, ed = read_staging(surreal, min_occurrence=2, max_entity=100, max_edge=100)

        surreal.list_entity_staging.assert_called_once_with(min_occurrence=2, limit=100)
        surreal.list_edge_staging.assert_called_once_with(min_occurrence=2, limit=100)
        assert e == entity_rows
        assert ed == edge_rows

    def test_empty_returns(self):
        surreal = _make_surreal_mock()
        e, ed = read_staging(surreal)
        assert e == []
        assert ed == []


# ── Tests: cluster_proposals ───────────────────────────────────────────────────


class TestClusterProposals:
    def _entity_row(self, proposed_role: str, occurrence: int = 1, n: int = 0) -> dict:
        return {
            "name": f"item-{proposed_role}-{n}",
            "proposed_role": proposed_role,
            "description": f"desc for {proposed_role} {n}",
            "occurrence_count": occurrence,
            "embedding": None,
        }

    def _edge_row(self, proposed_relation: str, occurrence: int = 1, n: int = 0) -> dict:
        return {
            "from_name": f"src-{n}",
            "from_role": "task",
            "to_name": f"tgt-{n}",
            "to_role": "artifact",
            "proposed_relation": proposed_relation,
            "description": f"desc for {proposed_relation} {n}",
            "occurrence_count": occurrence,
            "embedding": None,
        }

    def test_groups_by_proposed_role(self):
        rows = [
            self._entity_row("compute_job", n=0),
            self._entity_row("compute_job", n=1),
            self._entity_row("sample_batch", n=0),
        ]
        composed = _make_composed()
        clusters = cluster_proposals(rows, [], composed)
        keys = {c.proposed_key for c in clusters}
        assert "compute_job" in keys
        assert "sample_batch" in keys

    def test_known_entity_roles_excluded(self):
        composed = _make_composed()
        known_role = composed.entity_roles()[0]  # e.g. "task"
        rows = [self._entity_row(known_role, n=0)]
        clusters = cluster_proposals(rows, [], composed)
        assert not any(c.proposed_key == known_role for c in clusters)

    def test_known_edge_types_excluded(self):
        from services.memory.ontology.gardener import _to_snake
        from services.memory.ontology.edges import CORE_EDGE_TYPES
        composed = _make_composed()
        known_edge = _to_snake(CORE_EDGE_TYPES[0].__name__)
        rows = [self._edge_row(known_edge, n=0)]
        clusters = cluster_proposals([], rows, composed)
        assert not any(c.proposed_key == known_edge for c in clusters)

    def test_unknown_edge_included(self):
        rows = [self._edge_row("submits_to", n=0)]
        composed = _make_composed()
        clusters = cluster_proposals([], rows, composed)
        assert any(c.proposed_key == "submits_to" for c in clusters)

    def test_sorted_by_total_occurrences_desc(self):
        rows = [
            self._entity_row("rare_type", occurrence=1, n=0),
            self._entity_row("common_type", occurrence=10, n=0),
            self._entity_row("common_type", occurrence=5, n=1),
        ]
        composed = _make_composed()
        clusters = cluster_proposals(rows, [], composed)
        # common_type total = 15, rare_type total = 1
        assert clusters[0].proposed_key == "common_type"
        assert clusters[1].proposed_key == "rare_type"

    def test_cluster_size_and_occurrences(self):
        rows = [self._entity_row("new_type", occurrence=4, n=i) for i in range(3)]
        composed = _make_composed()
        clusters = cluster_proposals(rows, [], composed)
        c = next(x for x in clusters if x.proposed_key == "new_type")
        assert c.size == 3
        assert c.total_occurrences == 12

    def test_empty_inputs(self):
        composed = _make_composed()
        clusters = cluster_proposals([], [], composed)
        assert clusters == []

    def test_ignores_rows_without_proposed_key(self):
        rows = [{"name": "x", "proposed_role": "", "occurrence_count": 5, "embedding": None}]
        composed = _make_composed()
        clusters = cluster_proposals(rows, [], composed)
        assert clusters == []


# ── Tests: decide_cluster ──────────────────────────────────────────────────────


class TestDecideCluster:
    def _make_cluster(self, kind: str = "entity", key: str = "compute_job", size: int = 3):
        items = [
            {"name": f"item-{i}", "description": f"desc {i}", "occurrence_count": 2, "embedding": None}
            for i in range(size)
        ]
        return StagingCluster(kind=kind, proposed_key=key, items=items)

    def test_extend_decision(self):
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = _make_llm_mock({
            "action": "extend",
            "rationale": "Genuinely new operational concept.",
            "proposed_name": "ComputeJob",
            "proposed_fields": ["job_id", "queue"],
        })
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "extend"
        assert decision.proposed_name == "ComputeJob"
        assert "job_id" in decision.proposed_fields

    def test_map_decision(self):
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = _make_llm_mock({
            "action": "map",
            "rationale": "Same as task.",
            "mapped_to": "task",
        })
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "map"
        assert decision.mapped_to == "task"

    def test_hold_decision(self):
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = _make_llm_mock({"action": "hold", "rationale": "Not enough signal."})
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "hold"

    def test_invalid_action_falls_back_to_hold(self):
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = _make_llm_mock({"action": "invent_something", "rationale": "bad"})
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "hold"

    def test_no_json_in_response_falls_back_to_hold(self):
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = MagicMock()
        llm.complete.return_value = "Sorry, I cannot decide."
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "hold"

    def test_llm_error_falls_back_to_hold(self):
        from services.memory.llm_client import LLMError
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = MagicMock()
        llm.complete.side_effect = LLMError("timeout")
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "hold"
        assert "LLM unavailable" in decision.rationale

    def test_edge_cluster_extend_with_pairs(self):
        cluster = StagingCluster(
            kind="edge",
            proposed_key="submits_to",
            items=[
                {"from_name": "job", "from_role": "task", "to_name": "queue",
                 "to_role": "artifact", "occurrence_count": 4, "embedding": None}
            ],
        )
        composed = _make_composed()
        llm = _make_llm_mock({
            "action": "extend",
            "rationale": "New operational relationship.",
            "proposed_name": "SubmitsTo",
            "proposed_pairs": [["task", "artifact"]],
        })
        decision = decide_cluster(cluster, llm, composed)
        assert decision.action == "extend"
        assert ("task", "artifact") in decision.proposed_pairs

    def test_rationale_preserved(self):
        cluster = self._make_cluster()
        composed = _make_composed()
        llm = _make_llm_mock({"action": "hold", "rationale": "Weak signal only."})
        decision = decide_cluster(cluster, llm, composed)
        assert decision.rationale == "Weak signal only."


# ── Tests: apply_map_decision ──────────────────────────────────────────────────


class TestApplyMapDecision:
    def _entity_cluster(self, proposed_key: str = "compute_job") -> StagingCluster:
        return StagingCluster(
            kind="entity",
            proposed_key=proposed_key,
            items=[
                {"name": "job1", "description": "a job", "occurrence_count": 3,
                 "embedding": None, "data": {}},
                {"name": "job2", "description": "b job", "occurrence_count": 2,
                 "embedding": None, "data": {}},
            ],
        )

    def _edge_cluster(self, proposed_rel: str = "submits_to") -> StagingCluster:
        return StagingCluster(
            kind="edge",
            proposed_key=proposed_rel,
            items=[
                {"from_name": "job1", "from_role": "task", "to_name": "q1",
                 "to_role": "artifact", "occurrence_count": 3, "embedding": None},
            ],
        )

    def test_entity_map_calls_upsert(self):
        composed = _make_composed()
        surreal = _make_surreal_mock()
        cluster = self._entity_cluster()
        decision = GardenerDecision(action="map", cluster=cluster, mapped_to="task")
        migrated = apply_map_decision(decision, surreal, composed)
        assert migrated == 2
        assert surreal.upsert_entity.call_count == 2
        first_call = surreal.upsert_entity.call_args_list[0]
        assert first_call.kwargs["role"] == "task"
        assert first_call.kwargs["name"] == "job1"

    def test_entity_map_unknown_target_returns_zero(self):
        composed = _make_composed()
        surreal = _make_surreal_mock()
        cluster = self._entity_cluster()
        decision = GardenerDecision(action="map", cluster=cluster, mapped_to="nonexistent_role")
        migrated = apply_map_decision(decision, surreal, composed)
        assert migrated == 0
        surreal.upsert_entity.assert_not_called()

    def test_edge_map_calls_relate(self):
        composed = _make_composed()
        surreal = _make_surreal_mock()
        cluster = self._edge_cluster()
        # depends_on is a known core edge → snake = "depends_on"
        decision = GardenerDecision(action="map", cluster=cluster, mapped_to="DependsOn")
        migrated = apply_map_decision(decision, surreal, composed)
        assert migrated == 1
        surreal.relate.assert_called_once()

    def test_edge_map_unknown_target_returns_zero(self):
        composed = _make_composed()
        surreal = _make_surreal_mock()
        cluster = self._edge_cluster()
        decision = GardenerDecision(action="map", cluster=cluster, mapped_to="invented_edge")
        migrated = apply_map_decision(decision, surreal, composed)
        assert migrated == 0
        surreal.relate.assert_not_called()

    def test_surreal_error_skipped(self):
        from services.memory.surrealdb.client import SurrealError
        composed = _make_composed()
        surreal = _make_surreal_mock()
        surreal.upsert_entity.side_effect = SurrealError("db error")
        cluster = self._entity_cluster()
        decision = GardenerDecision(action="map", cluster=cluster, mapped_to="task")
        migrated = apply_map_decision(decision, surreal, composed)
        assert migrated == 0  # both items failed


# ── Tests: emit_proposal_mr (dry-run) ─────────────────────────────────────────


class TestEmitProposalMr:
    def _cluster(self, kind: str = "entity", key: str = "compute_job") -> StagingCluster:
        return StagingCluster(
            kind=kind,
            proposed_key=key,
            items=[
                {"name": "item1", "description": "a compute job", "occurrence_count": 5,
                 "from_role": "task", "to_role": "artifact"},
            ],
        )

    def test_dry_run_returns_marker_and_no_git(self):
        cluster = self._cluster()
        decision = GardenerDecision(
            action="extend",
            cluster=cluster,
            rationale="New concept.",
            proposed_name="ComputeJob",
            proposed_fields=["job_id"],
        )
        config = OntologyGardenerConfig(dry_run=True)
        result = emit_proposal_mr(decision, "test-group", config)
        assert result == "dry-run://mr/0"

    def test_dry_run_edge_cluster(self):
        cluster = self._cluster(kind="edge", key="submits_to")
        decision = GardenerDecision(
            action="extend",
            cluster=cluster,
            rationale="New edge.",
            proposed_name="SubmitsTo",
            proposed_pairs=[("task", "artifact")],
        )
        config = OntologyGardenerConfig(dry_run=True)
        result = emit_proposal_mr(decision, "test-group", config)
        assert result == "dry-run://mr/0"


# ── Tests: run_gardener (end-to-end with mocks) ────────────────────────────────


class TestRunGardener:
    def test_hold_all_clusters(self):
        entity_rows = [
            {"name": f"item-{i}", "proposed_role": "slurm_job",
             "description": f"desc {i}", "occurrence_count": 5, "embedding": None}
            for i in range(3)
        ]
        surreal = _make_surreal_mock(entity_rows=entity_rows)
        llm = _make_llm_mock({"action": "hold", "rationale": "Not enough signal."})
        registry = _make_registry()
        config = OntologyGardenerConfig(
            min_occurrence=1, cluster_size_threshold=1, dry_run=True
        )
        summary = run_gardener("test-group", surreal, llm, registry, config)
        assert summary["clusters_found"] == 1
        assert summary["hold"] == 1
        assert summary["extend"] == 0
        assert summary["map"] == 0

    def test_occurrence_threshold_filters(self):
        entity_rows = [
            {"name": "item1", "proposed_role": "rare_type",
             "description": "d", "occurrence_count": 1, "embedding": None},
        ]
        surreal = _make_surreal_mock(entity_rows=entity_rows)
        # Surreal returns the pre-filtered rows; filtering is done in list_entity_staging.
        # With min_occurrence=3 and the mock returning the row regardless,
        # we test that the cluster_size_threshold gate works.
        llm = _make_llm_mock({"action": "extend", "rationale": "r", "proposed_name": "RareType"})
        registry = _make_registry()
        config = OntologyGardenerConfig(
            min_occurrence=3, cluster_size_threshold=2, dry_run=True
        )
        # Only 1 item in the cluster → filtered by cluster_size_threshold=2.
        summary = run_gardener("test-group", surreal, llm, registry, config)
        assert summary["clusters_found"] == 0

    def test_extend_opens_mr(self):
        entity_rows = [
            {"name": f"job{i}", "proposed_role": "slurm_job",
             "description": "d", "occurrence_count": 5, "embedding": None}
            for i in range(2)
        ]
        surreal = _make_surreal_mock(entity_rows=entity_rows)
        llm = _make_llm_mock({
            "action": "extend",
            "rationale": "New operational concept.",
            "proposed_name": "SlurmJob",
            "proposed_fields": ["job_id"],
        })
        registry = _make_registry()
        config = OntologyGardenerConfig(
            min_occurrence=1, cluster_size_threshold=1, dry_run=True
        )
        summary = run_gardener("test-group", surreal, llm, registry, config)
        assert summary["extend"] == 1
        assert summary["mrs_opened"] == 1

    def test_map_migrates_items(self):
        entity_rows = [
            {"name": f"job{i}", "proposed_role": "compute_job",
             "description": "d", "occurrence_count": 5, "embedding": None, "data": {}}
            for i in range(2)
        ]
        surreal = _make_surreal_mock(entity_rows=entity_rows)
        llm = _make_llm_mock({"action": "map", "rationale": "Same as task.", "mapped_to": "task"})
        registry = _make_registry()
        config = OntologyGardenerConfig(
            min_occurrence=1, cluster_size_threshold=1, dry_run=True
        )
        summary = run_gardener("test-group", surreal, llm, registry, config)
        assert summary["map"] == 1
        assert summary["items_migrated"] == 2

    def test_no_staging_rows_returns_empty_summary(self):
        surreal = _make_surreal_mock()
        llm = MagicMock()
        registry = _make_registry()
        config = OntologyGardenerConfig(min_occurrence=1, cluster_size_threshold=1, dry_run=True)
        summary = run_gardener("test-group", surreal, llm, registry, config)
        assert summary["clusters_found"] == 0
        llm.complete.assert_not_called()

    def test_uses_default_config_when_none(self):
        surreal = _make_surreal_mock()
        llm = MagicMock()
        registry = _make_registry()
        # Should not raise; default config is instantiated internally.
        summary = run_gardener("test-group", surreal, llm, registry, config=None)
        assert "clusters_found" in summary


# ── Tests: helpers ─────────────────────────────────────────────────────────────


class TestHelpers:
    def test_cosine_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(_cosine(v, v) - 1.0) < 1e-9

    def test_cosine_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine(a, b)) < 1e-9

    def test_cosine_empty_vectors(self):
        assert _cosine([], []) == 0.0

    def test_cosine_zero_magnitude(self):
        assert _cosine([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_to_snake_camel(self):
        assert _to_snake("DependsOn") == "depends_on"
        assert _to_snake("TriggersDecision") == "triggers_decision"

    def test_to_camel_snake(self):
        assert _to_camel("compute_job") == "ComputeJob"
        assert _to_camel("slurm_job_id") == "SlurmJobId"

    def test_staging_cluster_sample_descriptions(self):
        items = [{"description": f"desc {i}", "embedding": None} for i in range(5)]
        cluster = StagingCluster(kind="entity", proposed_key="x", items=items)
        samples = cluster.sample_descriptions(3)
        assert len(samples) == 3
        assert samples[0] == "desc 0"

    def test_staging_cluster_size_and_occurrences(self):
        items = [{"occurrence_count": i + 1} for i in range(4)]
        cluster = StagingCluster(kind="entity", proposed_key="x", items=items)
        assert cluster.size == 4
        assert cluster.total_occurrences == 10  # 1+2+3+4


# ── Tests: SurrealClient staging methods ──────────────────────────────────────


class TestSurrealClientStagingMethods:
    """Test the new list_entity_staging / list_edge_staging methods via mock."""

    def _make_client(self, query_return=None):
        import services.memory.surrealdb.client as _mod
        _mod._schema_applied.discard("test-group")
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.signin = MagicMock(return_value=None)
        mock_db.use = MagicMock(return_value=None)
        rows = query_return if query_return is not None else []
        mock_db.query_raw = MagicMock(return_value={
            "result": [{"status": "OK", "result": rows}]
        })
        mock_db.check_response_for_error = MagicMock(return_value=None)
        mock_db.check_response_for_result = MagicMock(return_value=None)
        with patch("services.memory.surrealdb.client.Surreal", return_value=mock_db):
            from services.memory.surrealdb.client import SurrealClient
            client = SurrealClient(
                group_id="test-group", url="http://localhost:8000",
                user="root", password="secret",
            )
        return client, mock_db

    def test_list_entity_staging_sends_query(self):
        rows = [{"name": "e1", "proposed_role": "slurm_job", "occurrence_count": 5}]
        client, mock_db = self._make_client(query_return=rows)
        result = client.list_entity_staging(min_occurrence=3, limit=50)
        assert result == rows
        call_args = mock_db.query_raw.call_args
        sql = call_args[0][0] if call_args[0] else ""
        assert "entity_staging" in sql
        assert "occurrence_count" in sql

    def test_list_edge_staging_sends_query(self):
        rows = [{"from_name": "a", "proposed_relation": "submits_to", "occurrence_count": 4}]
        client, mock_db = self._make_client(query_return=rows)
        result = client.list_edge_staging(min_occurrence=2, limit=100)
        assert result == rows
        call_args = mock_db.query_raw.call_args
        sql = call_args[0][0] if call_args[0] else ""
        assert "edge_staging" in sql

    def test_list_entity_staging_empty_returns_list(self):
        client, _ = self._make_client(query_return=[])
        result = client.list_entity_staging()
        assert result == []

    def test_list_edge_staging_empty_returns_list(self):
        client, _ = self._make_client(query_return=[])
        result = client.list_edge_staging()
        assert result == []


# ── Live smoke tests (pytest.mark.live) ────────────────────────────────────────


@pytest.mark.live
class TestGardenerLiveSmoke:
    """Smoke tests against a real SurrealDB instance.

    Requires: SURREAL_URL, SURREAL_USER (default: root), SURREAL_PASS env vars.

    Run with:
        pytest -m live services/memory/tests/test_gardener.py

    Uses the isolated group_id ``wiki-gardener-smoke`` — safe to run repeatedly;
    records inserted are left in place (for post-run inspection) but the group_id
    is namespace-isolated from any production data.
    """

    SMOKE_GROUP = "wiki-gardener-smoke"

    def _surreal(self):
        from services.memory.surrealdb.client import SurrealClient
        return SurrealClient(group_id=self.SMOKE_GROUP)

    def test_read_staging_returns_seeded_rows(self):
        """Seed an entity_staging row and verify list_entity_staging returns it."""
        surreal = self._surreal()
        surreal.upsert_entity_staging(
            name="smoke-slurm-job",
            proposed_role="slurm_job",
            description="A SLURM batch job submitted to the HPC queue",
            rationale="live smoke test",
            provenance="test_gardener_live",
        )
        # Increment occurrence_count to meet default min_occurrence=3.
        for _ in range(2):
            surreal.upsert_entity_staging(
                name="smoke-slurm-job",
                proposed_role="slurm_job",
                description="A SLURM batch job submitted to the HPC queue",
                rationale="live smoke test",
                provenance="test_gardener_live",
            )

        rows = surreal.list_entity_staging(min_occurrence=3, limit=50)
        names = [r.get("name") for r in rows]
        assert "smoke-slurm-job" in names, f"Seeded row not found; got: {names}"

    def test_map_decision_migrates_staged_item_into_typed_store(self):
        """Seed a staging row, apply a Map decision, assert entity lands in typed store."""
        surreal = self._surreal()
        # Seed.
        surreal.upsert_entity_staging(
            name="smoke-map-entity",
            proposed_role="compute_step",
            description="A computation step that maps to the core step role",
            rationale="live smoke test map",
            provenance="test_gardener_live",
        )

        # Build a cluster with the seeded row.
        cluster = StagingCluster(
            kind="entity",
            proposed_key="compute_step",
            items=[{
                "name": "smoke-map-entity",
                "description": "A computation step that maps to the core step role",
                "occurrence_count": 1,
                "embedding": None,
                "data": {},
            }],
        )
        decision = GardenerDecision(
            action="map",
            cluster=cluster,
            rationale="compute_step maps to existing 'step' role",
            mapped_to="step",
        )

        registry = OntologyRegistry()
        composed = registry.compose(self.SMOKE_GROUP)
        migrated = apply_map_decision(decision, surreal, composed)
        assert migrated == 1, f"Expected 1 migrated, got {migrated}"

        # Verify the entity landed in the typed store.
        results = surreal.query(
            "SELECT * FROM entity WHERE role = $role AND name = $name",
            {"role": "step", "name": "smoke-map-entity"},
        )
        rows = results[0] if results else []
        assert isinstance(rows, list) and len(rows) >= 1, (
            "Migrated entity not found in typed store"
        )
        assert rows[0].get("role") == "step"
        assert rows[0].get("name") == "smoke-map-entity"
