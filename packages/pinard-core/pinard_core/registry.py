"""Ontology registry — composes pinard-core + per-repo domain ontologies.

The registry is the single source of truth for entity and edge types per
group_id (vigne/pipeline scope).  Domain ontologies subclass core types and
are registered per group_id; the registry returns the composed set for
Graphiti per-call entity_types/edge_types and SurrealDB relation names.

Usage::

    registry = OntologyRegistry()

    # Register a domain for a specific group_id
    registry.register_domain(
        group_id="genomics-build",
        entity_types=[SlurmJob, GWASStudy],
        edge_types=[],
        domain_name="genomics",
        domain_version="1.0.0",
    )

    # Compose core + domain for a group_id
    composed = registry.compose("genomics-build")
    composed.entity_types  # core types + domain types (minus suppressed)
    composed.edge_types    # core edge types + domain edge types (minus suppressed)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .edges import CORE_EDGE_TYPES, EDGE_TYPE_MAP, CoreEdge
from .entities import CORE_ENTITY_TYPES, CoreEntity
from .versioning import OntologyVersion

CORE_VERSION = "1.0.0"


@dataclass
class DomainOntology:
    """A per-repo domain ontology that extends pinard-core."""

    name: str
    version: str
    entity_types: list[type[CoreEntity]] = field(default_factory=list)
    edge_types: list[type[CoreEdge]] = field(default_factory=list)
    suppressed_types: set[str] = field(default_factory=set)
    # Extended edge-type map for domain-specific edges.
    edge_type_map_extension: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict
    )


@dataclass
class ComposedOntology:
    """The result of composing pinard-core + an optional domain.

    Attributes:
        entity_types: Active entity classes (core + domain, minus suppressed).
        edge_types: Active edge classes (core + domain, minus suppressed).
        edge_type_map: Merged edge-type map (core + domain extensions).
        version: Version stamp for this composition.
    """

    entity_types: list[type[CoreEntity]]
    edge_types: list[type[CoreEdge]]
    edge_type_map: dict[str, list[tuple[str, str]]]
    version: OntologyVersion

    def entity_roles(self) -> list[str]:
        """Return the list of active entity role strings."""
        roles = []
        for cls in self.entity_types:
            field = cls.model_fields.get("role")
            if field is not None:
                roles.append(field.default)
        return roles

    def edge_names(self) -> list[str]:
        """Return the list of active edge class names."""
        return [cls.__name__ for cls in self.edge_types]


class OntologyRegistry:
    """Registry that manages core + domain ontologies per group_id."""

    def __init__(self) -> None:
        self._domains: dict[str, DomainOntology] = {}

    def register_domain(
        self,
        group_id: str,
        entity_types: Optional[list[type[CoreEntity]]] = None,
        edge_types: Optional[list[type[CoreEdge]]] = None,
        domain_name: str = "",
        domain_version: str = "0.0.0",
        suppressed_types: Optional[set[str]] = None,
        edge_type_map_extension: Optional[dict[str, list[tuple[str, str]]]] = None,
    ) -> None:
        """Register (or replace) a domain ontology for *group_id*.

        Args:
            group_id: The vigne/pipeline scope this domain applies to.
            entity_types: Additional entity classes (subclasses of CoreEntity).
            edge_types: Additional edge classes (subclasses of CoreEdge).
            domain_name: Human-readable domain identifier (e.g. 'genomics').
            domain_version: Semver version of the domain ontology.
            suppressed_types: Set of type names (entity role strings or edge
                class names) to exclude from the composed ontology.
            edge_type_map_extension: Additional (source, target) pairs for new
                or extended edge types.
        """
        self._domains[group_id] = DomainOntology(
            name=domain_name or group_id,
            version=domain_version,
            entity_types=list(entity_types or []),
            edge_types=list(edge_types or []),
            suppressed_types=set(suppressed_types or set()),
            edge_type_map_extension=dict(edge_type_map_extension or {}),
        )

    def compose(self, group_id: str) -> ComposedOntology:
        """Return the composed ontology for *group_id*.

        If no domain has been registered for *group_id* the result is the
        pure pinard-core ontology.
        """
        domain = self._domains.get(group_id)
        suppressed = domain.suppressed_types if domain else set()

        # --- Entity types ---
        all_entities: list[type[CoreEntity]] = []
        for cls in CORE_ENTITY_TYPES:
            role = cls.model_fields["role"].default
            if role not in suppressed and cls.__name__ not in suppressed:
                all_entities.append(cls)
        if domain:
            for cls in domain.entity_types:
                field = cls.model_fields.get("role")
                role_val = field.default if field is not None else cls.__name__
                if role_val not in suppressed and cls.__name__ not in suppressed:
                    all_entities.append(cls)

        # --- Edge types ---
        all_edges: list[type[CoreEdge]] = []
        for cls in CORE_EDGE_TYPES:
            if cls.__name__ not in suppressed:
                all_edges.append(cls)
        if domain:
            for cls in domain.edge_types:
                if cls.__name__ not in suppressed:
                    all_edges.append(cls)

        # --- Edge type map ---
        merged_map: dict[str, list[tuple[str, str]]] = {
            k: list(v) for k, v in EDGE_TYPE_MAP.items()
        }
        if domain:
            for edge_name, pairs in domain.edge_type_map_extension.items():
                if edge_name in merged_map:
                    merged_map[edge_name] = merged_map[edge_name] + list(pairs)
                else:
                    merged_map[edge_name] = list(pairs)

        # --- Version ---
        version = OntologyVersion(
            core_version=CORE_VERSION,
            domain_name=domain.name if domain else None,
            domain_version=domain.version if domain else None,
        )

        return ComposedOntology(
            entity_types=all_entities,
            edge_types=all_edges,
            edge_type_map=merged_map,
            version=version,
        )

    def registered_groups(self) -> list[str]:
        """Return the list of group_ids that have a registered domain."""
        return list(self._domains.keys())

    def get_domain(self, group_id: str) -> Optional[DomainOntology]:
        """Return the registered domain for *group_id*, or None."""
        return self._domains.get(group_id)
