"""Pinard ontology — pinard-core re-exports and the ontology gardener.

Core types (CoreEntity, CoreEdge, OntologyRegistry, …) are re-exported from
the ``pinard_core`` package.  The gardener lives here as a memory-service
component.
"""
from pinard_core import *  # noqa: F401, F403
from pinard_core import (
    CORE_EDGE_BY_NAME,
    CORE_EDGE_TYPES,
    CORE_ENTITY_BY_ROLE,
    CORE_ENTITY_TYPES,
    EDGE_TYPE_MAP,
    Action,
    Artifact,
    ComposedOntology,
    Consumes,
    CoreEdge,
    CoreEntity,
    Decision,
    Diagnosis,
    DomainOntology,
    DependsOn,
    EnvironmentCondition,
    Gate,
    IndicatesProblem,
    LogPattern,
    MigrationError,
    MigrationPolicy,
    MigrationResult,
    OntologyRegistry,
    OntologyVersion,
    Produces,
    RequiresCondition,
    ResolvedBy,
    Step,
    Task,
    TriggersDecision,
    Verdict,
)
from .gardener import (  # noqa: E402
    GardenerDecision,
    OntologyGardenerConfig,
    StagingCluster,
    apply_map_decision,
    cluster_proposals,
    decide_cluster,
    emit_proposal_mr,
    read_staging,
    run_gardener,
)
