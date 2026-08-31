"""Compatibility shim — re-exports from pinard_core."""
from pinard_core.edges import *  # noqa: F401, F403
from pinard_core.edges import (
    CORE_EDGE_BY_NAME,
    CORE_EDGE_TYPES,
    EDGE_TYPE_MAP,
    Consumes,
    CoreEdge,
    DependsOn,
    IndicatesProblem,
    Produces,
    RequiresCondition,
    ResolvedBy,
    TriggersDecision,
)
