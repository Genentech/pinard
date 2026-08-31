"""pinard-core entity models — repo-agnostic agent-operational concepts.

Mirrors babysitter primitives. Each entity is a Pydantic BaseModel that
domain ontologies may subclass to extend with repo-specific fields.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CORE_ENTITY_VERSION = "1.0.0"


class CoreEntity(BaseModel):
    """Base class for all pinard-core entities."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Canonical name of this entity instance")
    description: str = Field("", description="Human-readable description")
    version: str = Field(CORE_ENTITY_VERSION, description="Entity type schema version")
    role: str = Field("", description="Operational role within the agent workflow")


class Task(CoreEntity):
    """A unit of work dispatched to a babysitter process.

    Mirrors the babysitter Task primitive — top-level work item with an
    effect ID, owned by one process run.
    """

    role: str = "task"
    effect_id: str = Field("", description="Babysitter effect ID (effectId)")
    process: str = Field("", description="Babysitter process name (e.g. 'swe')")
    status: str = Field(
        "pending",
        description="Lifecycle status: pending | running | done | failed",
    )


class Step(CoreEntity):
    """A sub-unit within a Task — one concrete action the agent takes."""

    role: str = "step"
    task_name: str = Field("", description="Name of the parent Task")
    tool: str = Field("", description="Tool invoked in this step (e.g. 'bash')")
    outcome: str = Field("", description="Short description of the step result")


class Verdict(CoreEntity):
    """A binary or graded judgment produced by the agent or the harness."""

    role: str = "verdict"
    passed: bool = Field(..., description="True if the verdict is positive/passing")
    rationale: str = Field("", description="Explanation for the verdict")
    confidence: float = Field(
        1.0, ge=0.0, le=1.0, description="Confidence score [0, 1]"
    )


class Decision(CoreEntity):
    """A deliberate choice made by the agent at a decision point."""

    role: str = "decision"
    options_considered: list = Field(
        default_factory=list,
        description="List of option strings that were evaluated",
    )
    chosen: str = Field("", description="The option that was selected")
    rationale: str = Field("", description="Reasoning behind the choice")


class Gate(CoreEntity):
    """A breakpoint or checkpoint that must be cleared before work continues.

    Equivalent to a babysitter gate / approval step.
    """

    role: str = "gate"
    condition: str = Field("", description="Condition that must hold to pass")
    passed: bool | None = Field(None, description="None = not yet evaluated")


class Action(CoreEntity):
    """A concrete action executed by the agent (tool call, command, API call)."""

    role: str = "action"
    tool: str = Field("", description="Tool or command name")
    parameters: dict = Field(default_factory=dict, description="Input parameters")
    result_summary: str = Field("", description="Short summary of the action result")
    success: bool | None = Field(None, description="None = not yet executed")


class Diagnosis(CoreEntity):
    """An identified root cause or failure mode, typically from log analysis."""

    role: str = "diagnosis"
    symptom: str = Field("", description="Observable symptom that triggered diagnosis")
    root_cause: str = Field("", description="Inferred root cause")
    confidence: float = Field(
        1.0, ge=0.0, le=1.0, description="Confidence score [0, 1]"
    )


class LogPattern(CoreEntity):
    """A recurring pattern in logs that signals a known condition."""

    role: str = "log_pattern"
    pattern: str = Field("", description="Regex or literal pattern string")
    signals: str = Field("", description="What condition this pattern signals")
    example: str = Field("", description="Representative log line example")


class EnvironmentCondition(CoreEntity):
    """An observable state of the execution environment (memory, disk, cluster)."""

    role: str = "environment_condition"
    metric: str = Field("", description="Metric name (e.g. 'memory_used_gb')")
    value: str = Field("", description="Observed value as string")
    threshold: str = Field("", description="Threshold that defines the condition")
    breached: bool = Field(False, description="True if the threshold was exceeded")


class Artifact(CoreEntity):
    """A durable output produced or consumed by agent work (file, dataset, model)."""

    role: str = "artifact"
    path: str = Field("", description="Filesystem or object-store path")
    artifact_type: str = Field(
        "", description="Type label (e.g. 'parquet', 'checkpoint', 'report')"
    )
    size_bytes: int = Field(0, ge=0, description="Size in bytes (0 = unknown)")


# Ordered list of all core entity classes — used by the registry.
CORE_ENTITY_TYPES: list[type[CoreEntity]] = [
    Task,
    Step,
    Verdict,
    Decision,
    Gate,
    Action,
    Diagnosis,
    LogPattern,
    EnvironmentCondition,
    Artifact,
]

# Mapping from role string → class, for fast look-up.
CORE_ENTITY_BY_ROLE: dict[str, type[CoreEntity]] = {
    cls.model_fields["role"].default: cls  # type: ignore[index]
    for cls in CORE_ENTITY_TYPES
}
