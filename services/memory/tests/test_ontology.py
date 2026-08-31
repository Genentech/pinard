"""Unit tests for the pinard-core ontology.

Covers:
- Base entity model field validation
- Base edge model field validation
- Edge-type map completeness (all entity roles appear in at least one pair)
- Core + domain composition via OntologyRegistry
- suppressed_types filtering
- OntologyVersion stamping and compatibility checks
- MigrationPolicy behaviour
"""
from __future__ import annotations

import sys
import os

# Ensure the repo root (services/) is importable when running from repo root.
# pinard_core is expected to be installed (pip install -e packages/pinard-core
# for in-repo dev, or pinard-core>=1.0 from the internal PyPI in prod/CI).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest

from pydantic import ValidationError

from services.memory.ontology.entities import (
    CORE_ENTITY_BY_ROLE,
    CORE_ENTITY_TYPES,
    Action,
    Artifact,
    CoreEntity,
    Decision,
    Diagnosis,
    EnvironmentCondition,
    Gate,
    LogPattern,
    Step,
    Task,
    Verdict,
)
from services.memory.ontology.edges import (
    CORE_EDGE_BY_NAME,
    CORE_EDGE_TYPES,
    EDGE_TYPE_MAP,
    Consumes,
    DependsOn,
    IndicatesProblem,
    Produces,
    RequiresCondition,
    ResolvedBy,
    TriggersDecision,
)
from services.memory.ontology.registry import OntologyRegistry
from services.memory.ontology.versioning import (
    MigrationError,
    MigrationPolicy,
    OntologyVersion,
)


# ---------------------------------------------------------------------------
# Entity model tests
# ---------------------------------------------------------------------------


class TestCoreEntityModels:
    def test_task_defaults(self):
        t = Task(name="deploy-step5")
        assert t.name == "deploy-step5"
        assert t.role == "task"
        assert t.status == "pending"
        assert t.description == ""

    def test_task_full_fields(self):
        t = Task(
            name="deploy",
            effect_id="01ABC",
            process="swe",
            status="running",
            description="Run the deploy step",
        )
        assert t.effect_id == "01ABC"
        assert t.process == "swe"
        assert t.status == "running"

    def test_step_defaults(self):
        s = Step(name="git-fetch")
        assert s.role == "step"
        assert s.task_name == ""
        assert s.tool == ""

    def test_verdict_requires_passed(self):
        v = Verdict(name="ci-check", passed=True)
        assert v.passed is True
        assert v.confidence == 1.0

    def test_verdict_confidence_bounds(self):
        with pytest.raises(ValidationError):
            Verdict(name="bad", passed=True, confidence=1.5)
        with pytest.raises(ValidationError):
            Verdict(name="bad", passed=True, confidence=-0.1)

    def test_decision_defaults(self):
        d = Decision(name="choose-model")
        assert d.options_considered == []
        assert d.chosen == ""

    def test_gate_passed_nullable(self):
        g = Gate(name="approval")
        assert g.passed is None
        g2 = Gate(name="approval", passed=True)
        assert g2.passed is True

    def test_action_defaults(self):
        a = Action(name="run-bash")
        assert a.parameters == {}
        assert a.success is None

    def test_diagnosis_confidence(self):
        diag = Diagnosis(name="oom-diagnosis", confidence=0.9)
        assert diag.confidence == 0.9

    def test_log_pattern_fields(self):
        lp = LogPattern(name="oom-log", pattern=r"Killed process \d+")
        assert lp.pattern == r"Killed process \d+"
        assert lp.role == "log_pattern"

    def test_environment_condition_fields(self):
        ec = EnvironmentCondition(name="high-mem", metric="memory_used_gb", value="95", threshold="90", breached=True)
        assert ec.breached is True

    def test_artifact_fields(self):
        art = Artifact(name="output-parquet", path="/data/out.parquet", artifact_type="parquet", size_bytes=1024)
        assert art.size_bytes == 1024
        assert art.artifact_type == "parquet"

    def test_artifact_size_non_negative(self):
        with pytest.raises(ValidationError):
            Artifact(name="bad", size_bytes=-1)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            Task(name="x", unexpected_field="y")

    def test_core_entity_types_list(self):
        assert len(CORE_ENTITY_TYPES) == 10
        roles = {cls.model_fields["role"].default for cls in CORE_ENTITY_TYPES}
        expected_roles = {
            "task", "step", "verdict", "decision", "gate",
            "action", "diagnosis", "log_pattern", "environment_condition", "artifact",
        }
        assert roles == expected_roles

    def test_core_entity_by_role_lookup(self):
        assert CORE_ENTITY_BY_ROLE["task"] is Task
        assert CORE_ENTITY_BY_ROLE["artifact"] is Artifact
        assert len(CORE_ENTITY_BY_ROLE) == 10


# ---------------------------------------------------------------------------
# Edge model tests
# ---------------------------------------------------------------------------


class TestCoreEdgeModels:
    def test_depends_on_requires_source_target(self):
        edge = DependsOn(source_type="task", target_type="gate")
        assert edge.source_type == "task"
        assert edge.target_type == "gate"

    def test_edge_confidence_default(self):
        edge = Produces(source_type="step", target_type="artifact")
        assert edge.confidence == 1.0

    def test_edge_confidence_bounds(self):
        with pytest.raises(ValidationError):
            Consumes(source_type="task", target_type="artifact", confidence=2.0)

    def test_all_core_edge_types_present(self):
        assert len(CORE_EDGE_TYPES) == 7
        names = {cls.__name__ for cls in CORE_EDGE_TYPES}
        expected = {
            "DependsOn", "Produces", "Consumes",
            "IndicatesProblem", "ResolvedBy", "RequiresCondition", "TriggersDecision",
        }
        assert names == expected

    def test_core_edge_by_name(self):
        assert CORE_EDGE_BY_NAME["DependsOn"] is DependsOn
        assert CORE_EDGE_BY_NAME["TriggersDecision"] is TriggersDecision


# ---------------------------------------------------------------------------
# Edge-type map completeness
# ---------------------------------------------------------------------------


class TestEdgeTypeMap:
    def test_all_core_edge_names_in_map(self):
        """Every core edge class must appear in EDGE_TYPE_MAP."""
        for cls in CORE_EDGE_TYPES:
            assert cls.__name__ in EDGE_TYPE_MAP, f"{cls.__name__} missing from EDGE_TYPE_MAP"

    def test_all_entity_roles_appear_in_at_least_one_pair(self):
        """Every core entity role must appear as a source or target in some edge pair."""
        all_roles = {cls.model_fields["role"].default for cls in CORE_ENTITY_TYPES}
        roles_in_map: set[str] = set()
        for pairs in EDGE_TYPE_MAP.values():
            for src, tgt in pairs:
                roles_in_map.add(src)
                roles_in_map.add(tgt)
        missing = all_roles - roles_in_map
        assert not missing, f"Entity roles not referenced in any edge pair: {missing}"

    def test_all_pairs_reference_valid_roles(self):
        """All roles in EDGE_TYPE_MAP must correspond to a known core entity role."""
        known_roles = {cls.model_fields["role"].default for cls in CORE_ENTITY_TYPES}
        for edge_name, pairs in EDGE_TYPE_MAP.items():
            for src, tgt in pairs:
                assert src in known_roles, (
                    f"Unknown source role '{src}' in {edge_name}"
                )
                assert tgt in known_roles, (
                    f"Unknown target role '{tgt}' in {edge_name}"
                )

    def test_each_edge_has_at_least_one_pair(self):
        for edge_name, pairs in EDGE_TYPE_MAP.items():
            assert len(pairs) >= 1, f"{edge_name} has no valid pairs"


# ---------------------------------------------------------------------------
# Registry and composition
# ---------------------------------------------------------------------------


class TestOntologyRegistry:
    def test_core_only_composition(self):
        registry = OntologyRegistry()
        composed = registry.compose("unknown-group")
        assert len(composed.entity_types) == len(CORE_ENTITY_TYPES)
        assert len(composed.edge_types) == len(CORE_EDGE_TYPES)
        assert composed.version.core_version == "1.0.0"
        assert composed.version.domain_name is None

    def test_domain_registration_and_composition(self):
        from services.memory.ontology.entities import Task as CoreTask

        class SlurmJob(CoreTask):
            role: str = "task"
            slurm_job_id: str = ""

        registry = OntologyRegistry()
        registry.register_domain(
            group_id="genomics-build",
            entity_types=[SlurmJob],
            domain_name="genomics",
            domain_version="1.0.0",
        )
        composed = registry.compose("genomics-build")
        # Should have core types + SlurmJob
        assert len(composed.entity_types) == len(CORE_ENTITY_TYPES) + 1
        assert SlurmJob in composed.entity_types
        assert composed.version.domain_name == "genomics"
        assert composed.version.domain_version == "1.0.0"

    def test_suppressed_types_excluded(self):
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="minimal-group",
            suppressed_types={"artifact", "log_pattern"},
        )
        composed = registry.compose("minimal-group")
        roles = composed.entity_roles()
        assert "artifact" not in roles
        assert "log_pattern" not in roles
        # Remaining core types should still be present
        assert "task" in roles

    def test_suppressed_edge_type_excluded(self):
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="no-consume-group",
            suppressed_types={"Consumes"},
        )
        composed = registry.compose("no-consume-group")
        edge_names = composed.edge_names()
        assert "Consumes" not in edge_names
        assert "DependsOn" in edge_names

    def test_edge_type_map_extension(self):
        registry = OntologyRegistry()
        registry.register_domain(
            group_id="extended-group",
            edge_type_map_extension={
                "DependsOn": [("artifact", "artifact")],
                "CustomEdge": [("task", "artifact")],
            },
        )
        composed = registry.compose("extended-group")
        # Existing edge extended
        assert ("artifact", "artifact") in composed.edge_type_map["DependsOn"]
        # New edge added
        assert ("task", "artifact") in composed.edge_type_map["CustomEdge"]

    def test_registered_groups(self):
        registry = OntologyRegistry()
        registry.register_domain("group-a")
        registry.register_domain("group-b")
        assert set(registry.registered_groups()) == {"group-a", "group-b"}

    def test_get_domain_returns_none_for_unknown(self):
        registry = OntologyRegistry()
        assert registry.get_domain("nonexistent") is None

    def test_domain_override(self):
        registry = OntologyRegistry()
        registry.register_domain("grp", domain_version="1.0.0")
        registry.register_domain("grp", domain_version="2.0.0")
        assert registry.get_domain("grp").version == "2.0.0"

    def test_entity_roles_helper(self):
        registry = OntologyRegistry()
        composed = registry.compose("any")
        roles = composed.entity_roles()
        assert "task" in roles
        assert "artifact" in roles
        assert len(roles) == len(CORE_ENTITY_TYPES)

    def test_edge_names_helper(self):
        registry = OntologyRegistry()
        composed = registry.compose("any")
        names = composed.edge_names()
        assert "DependsOn" in names
        assert len(names) == len(CORE_EDGE_TYPES)


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


class TestOntologyVersioning:
    def test_version_stamp_core_only(self):
        v = OntologyVersion(core_version="1.0.0")
        stamp = v.as_stamp()
        assert stamp == {"pinard_core": "1.0.0"}

    def test_version_stamp_with_domain(self):
        v = OntologyVersion(core_version="1.0.0", domain_name="genomics", domain_version="1.2.3")
        stamp = v.as_stamp()
        assert stamp["pinard_core"] == "1.0.0"
        assert stamp["domain"]["name"] == "genomics"
        assert stamp["domain"]["version"] == "1.2.3"

    def test_version_stamp_domain_default_version(self):
        v = OntologyVersion(core_version="1.0.0", domain_name="genomics")
        stamp = v.as_stamp()
        assert stamp["domain"]["version"] == "0.0.0"

    def test_compatibility_same_major(self):
        v1 = OntologyVersion(core_version="1.0.0")
        v2 = OntologyVersion(core_version="1.5.3")
        assert v1.is_compatible_with(v2) is True

    def test_incompatibility_different_major(self):
        v1 = OntologyVersion(core_version="1.0.0")
        v2 = OntologyVersion(core_version="2.0.0")
        assert v1.is_compatible_with(v2) is False


class TestMigrationPolicy:
    def test_no_change(self):
        policy = MigrationPolicy(
            from_version=OntologyVersion(core_version="1.0.0"),
            to_version=OntologyVersion(core_version="1.0.0"),
        )
        result = policy.check()
        assert result.needed is False
        assert result.safe is True

    def test_minor_bump_safe(self):
        policy = MigrationPolicy(
            from_version=OntologyVersion(core_version="1.0.0"),
            to_version=OntologyVersion(core_version="1.1.0"),
        )
        result = policy.check()
        assert result.needed is True
        assert result.safe is True

    def test_major_bump_unsafe(self):
        policy = MigrationPolicy(
            from_version=OntologyVersion(core_version="1.0.0"),
            to_version=OntologyVersion(core_version="2.0.0"),
        )
        result = policy.check()
        assert result.needed is True
        assert result.safe is False

    def test_apply_noop_for_safe_migration(self):
        policy = MigrationPolicy(
            from_version=OntologyVersion(core_version="1.0.0"),
            to_version=OntologyVersion(core_version="1.1.0"),
        )
        policy.apply()  # should not raise

    def test_apply_raises_for_unsafe_migration(self):
        policy = MigrationPolicy(
            from_version=OntologyVersion(core_version="1.0.0"),
            to_version=OntologyVersion(core_version="2.0.0"),
        )
        with pytest.raises(MigrationError):
            policy.apply()
