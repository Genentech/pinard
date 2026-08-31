"""pinard-core edge models and edge-type map.

Each edge model captures a typed relationship between two core entities.
The EDGE_TYPE_MAP encodes which (source, target) type pairs are valid for
each edge kind — used by the registry to validate domain compositions and
by the SurrealDB ingester to generate RELATE statements.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .entities import CoreEntity


class CoreEdge(BaseModel):
    """Base class for all pinard-core edges."""

    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(..., description="Role of the source entity")
    target_type: str = Field(..., description="Role of the target entity")
    description: str = Field("", description="Human-readable relationship description")
    confidence: float = Field(
        1.0, ge=0.0, le=1.0, description="Confidence score [0, 1]"
    )


# ---------------------------------------------------------------------------
# Core edge types
# ---------------------------------------------------------------------------


class DependsOn(CoreEdge):
    """Entity A depends on entity B before it can proceed."""

    source_type: str = Field(..., description="Dependent entity role")
    target_type: str = Field(..., description="Prerequisite entity role")


class Produces(CoreEdge):
    """Entity A produces entity B as an output."""

    source_type: str = Field(..., description="Producer entity role")
    target_type: str = Field(..., description="Produced entity role")


class Consumes(CoreEdge):
    """Entity A consumes entity B as an input."""

    source_type: str = Field(..., description="Consumer entity role")
    target_type: str = Field(..., description="Consumed entity role")


class IndicatesProblem(CoreEdge):
    """Entity A (e.g. LogPattern) indicates a problem entity B (e.g. Diagnosis)."""

    source_type: str = Field(..., description="Indicator entity role")
    target_type: str = Field(..., description="Problem entity role")


class ResolvedBy(CoreEdge):
    """Entity A (problem/Diagnosis) is resolved by entity B (Action/Step)."""

    source_type: str = Field(..., description="Problem entity role")
    target_type: str = Field(..., description="Resolution entity role")


class RequiresCondition(CoreEdge):
    """Entity A requires EnvironmentCondition B to hold."""

    source_type: str = Field(..., description="Requiring entity role")
    target_type: str = Field(..., description="Condition entity role")


class TriggersDecision(CoreEdge):
    """Entity A (Gate, EnvironmentCondition, Verdict) triggers Decision B."""

    source_type: str = Field(..., description="Triggering entity role")
    target_type: str = Field(..., description="Decision entity role")


# ---------------------------------------------------------------------------
# Edge-type map
#
# Maps edge class name → list of valid (source_role, target_role) pairs.
# A pair is valid if it is semantically meaningful and backed by the spec.
# Multiple pairs per edge are allowed — an edge type can connect different
# kinds of entities.
# ---------------------------------------------------------------------------

EdgeTypePairs = list[tuple[str, str]]

EDGE_TYPE_MAP: dict[str, EdgeTypePairs] = {
    "DependsOn": [
        ("task", "task"),
        ("task", "gate"),
        ("step", "step"),
        ("step", "artifact"),
        ("action", "artifact"),
        ("action", "environment_condition"),
    ],
    "Produces": [
        ("task", "artifact"),
        ("step", "artifact"),
        ("action", "artifact"),
        ("action", "verdict"),
        ("step", "verdict"),
    ],
    "Consumes": [
        ("task", "artifact"),
        ("step", "artifact"),
        ("action", "artifact"),
        ("action", "environment_condition"),
    ],
    "IndicatesProblem": [
        ("log_pattern", "diagnosis"),
        ("environment_condition", "diagnosis"),
        ("verdict", "diagnosis"),
    ],
    "ResolvedBy": [
        ("diagnosis", "action"),
        ("diagnosis", "step"),
        ("diagnosis", "task"),
    ],
    "RequiresCondition": [
        ("task", "environment_condition"),
        ("step", "environment_condition"),
        ("gate", "environment_condition"),
        ("action", "environment_condition"),
    ],
    "TriggersDecision": [
        ("gate", "decision"),
        ("environment_condition", "decision"),
        ("verdict", "decision"),
        ("diagnosis", "decision"),
    ],
}

# All core edge classes in canonical order.
CORE_EDGE_TYPES: list[type[CoreEdge]] = [
    DependsOn,
    Produces,
    Consumes,
    IndicatesProblem,
    ResolvedBy,
    RequiresCondition,
    TriggersDecision,
]

CORE_EDGE_BY_NAME: dict[str, type[CoreEdge]] = {
    cls.__name__: cls for cls in CORE_EDGE_TYPES
}
