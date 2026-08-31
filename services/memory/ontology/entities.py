"""Compatibility shim — re-exports from pinard_core."""
from pinard_core.entities import *  # noqa: F401, F403
from pinard_core.entities import (
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
