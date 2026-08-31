"""pinard-core ontology — public API surface."""

from .edges import (
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
from .entities import (
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
from .registry import ComposedOntology, DomainOntology, OntologyRegistry
from .versioning import (
    MigrationError,
    MigrationPolicy,
    MigrationResult,
    OntologyVersion,
)

__all__ = [
    # Entities
    "CoreEntity",
    "Task",
    "Step",
    "Verdict",
    "Decision",
    "Gate",
    "Action",
    "Diagnosis",
    "LogPattern",
    "EnvironmentCondition",
    "Artifact",
    "CORE_ENTITY_TYPES",
    "CORE_ENTITY_BY_ROLE",
    # Edges
    "CoreEdge",
    "DependsOn",
    "Produces",
    "Consumes",
    "IndicatesProblem",
    "ResolvedBy",
    "RequiresCondition",
    "TriggersDecision",
    "CORE_EDGE_TYPES",
    "CORE_EDGE_BY_NAME",
    "EDGE_TYPE_MAP",
    # Registry
    "OntologyRegistry",
    "DomainOntology",
    "ComposedOntology",
    # Versioning
    "OntologyVersion",
    "MigrationPolicy",
    "MigrationResult",
    "MigrationError",
]
