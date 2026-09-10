## ADDED Requirements

### Requirement: Declarative Core Ontology

The pinard core ontology (entity roles, edge types, property schemas) SHALL be
encoded as declarative YAML/JSON data embedded in the pinard binary. It MUST NOT
require Python or any interpreted runtime.

#### Scenario: Core ontology loads on startup
- **WHEN** any memory binary starts
- **THEN** `ontology.NewRegistry()` loads the embedded `core.yaml` with 10 entity roles
  and 7 edge types without error

#### Scenario: Core ontology is self-contained
- **GIVEN** the pinard binary with no external files
- **WHEN** `Compose("any-group")` is called
- **THEN** the result contains all 10 core entity roles and 7 edge types

### Requirement: Ontology File Envelope

A domain ontology file SHALL conform to the pinard meta-schema:
- Top-level required fields: `domain` (string), `version` (semver X.Y.Z),
  `entities` (object), `edges` (object).
- Optional: `group_ids` (list of strings), `suppressed` (list of role/edge names).
- Each entity entry requires `description`; optional: `is_a`, `properties`, `required`.
- Each edge entry requires `pairs` (list of `[source_role, target_role]` tuples).
- Property definitions are literal JSON Schema (type, description, format,
  minimum, maximum, enum, items).

#### Scenario: Valid file passes meta-schema
- **WHEN** a conformant domain YAML is passed to `aoc ontology validate <file>`
- **THEN** the command exits 0 and prints `✓ <file> is valid`

#### Scenario: Missing required field is rejected
- **WHEN** a domain file omits `version`
- **THEN** `aoc ontology validate` exits non-zero with a descriptive error message

### Requirement: Domain Extension Loading

Domain ontologies SHALL be discovered at runtime from:
1. Directories listed in `PINARD_ONTOLOGY_DIRS` (colon-separated).
2. `<vignoble>/pinard/ontology/*.{yaml,yml,json}` (when `VIGNOBLE_DIR` is set).

No pinard rebuild SHALL be required to add a domain.

#### Scenario: Domain YAML loaded from PINARD_ONTOLOGY_DIRS
- **GIVEN** `PINARD_ONTOLOGY_DIRS=/path/to/ontologies`
- **AND** `/path/to/ontologies/example.yaml` with `group_ids: [example-build]`
- **WHEN** the memory-ingester starts
- **THEN** `Compose("example-build")` includes the example entity roles

#### Scenario: Missing directory is non-fatal
- **GIVEN** `PINARD_ONTOLOGY_DIRS=/nonexistent/path`
- **WHEN** the registry loads
- **THEN** a warning is logged but the process starts successfully with core-only ontology

#### Scenario: Invalid file is reported, others still load
- **GIVEN** a directory with one valid and one malformed YAML
- **WHEN** the registry loads
- **THEN** the valid file is loaded and the malformed file produces a logged warning

### Requirement: Composition Semantics

`Compose(group_id)` SHALL return a `ComposedOntology` equal to:
  `core + domain(group_id) − suppressed`

With:
- **Entities**: core entities merged with domain entities; domain entities with `is_a`
  inherit ancestor properties (domain properties override core properties for the
  same key).
- **Edges**: core edges merged with domain edges; if a domain file defines an edge
  that shares a name with a core edge, the domain pairs are **appended** (not replaced).
- **Suppressed**: any role or edge name in either `suppressed` list is excluded from
  the result.
- **Version**: `OntologyVersion{CoreVersion, DomainName, DomainVersion}`.

#### Scenario: Domain entity inherits parent properties
- **GIVEN** a domain entity `slurm_job` with `is_a: task`
- **WHEN** `Compose("example-build")` is called
- **THEN** `slurm_job` has both its own fields and the `task` fields (`effect_id`, `process`, `status`)

#### Scenario: Domain extends core edge
- **GIVEN** a domain file adding pairs to `DependsOn`
- **WHEN** `Compose("example-build")` is called
- **THEN** `EdgeTypeMap["DependsOn"]` contains both core pairs and the domain extension

#### Scenario: Suppressed entity is absent
- **GIVEN** a domain file with `suppressed: [verdict]`
- **WHEN** `Compose(group_id)` is called
- **THEN** the result has no `verdict` entity

#### Scenario: Compose result is cached
- **WHEN** `Compose` is called twice with the same group_id
- **THEN** the same pointer is returned (no recomputation)

### Requirement: SurrealDB DDL Generation

`ontology.GenerateSurrealDDL(composed)` SHALL produce valid SurrealDB DDL that:
- Defines the `entity` SCHEMAFULL table with base fields.
- Defines `data.<property>` fields for each entity's properties, mapping JSON Schema
  types to SurrealDB types (`string→string`, `integer→int`, `number→float`,
  `boolean→bool`, `string+format:date-time→datetime`, `array→array`, `object→object FLEXIBLE`).
- Generates ASSERT constraints for `minimum`/`maximum`/`enum`.
- Defines a SCHEMAFULL RELATION table for each edge in `EdgeTypeMap`.

#### Scenario: Core edges produce RELATION tables
- **GIVEN** a composed core-only ontology
- **WHEN** `GenerateSurrealDDL` is called
- **THEN** the result contains `DEFINE TABLE IF NOT EXISTS depends_on` and
  `DEFINE TABLE IF NOT EXISTS triggers_decision`

#### Scenario: Domain edges appear in DDL
- **GIVEN** a composed ontology with domain edge `ProcessesStudy`
- **WHEN** `GenerateSurrealDDL` is called
- **THEN** the result contains `DEFINE TABLE IF NOT EXISTS processes_study`

### Requirement: Ingester Role Validation

The memory-ingester SHALL derive entity roles from the composed ontology (not a
hardcoded set). Observations whose mapped role is not active in the composed ontology
SHALL be routed to `entity_staging` for gardener review.

#### Scenario: Known role upserted directly
- **GIVEN** a composed ontology with role `decision`
- **AND** an Engram observation with `obs_type: "rule"`
- **WHEN** the ingester processes the observation
- **THEN** `UpsertEntity("decision", ...)` is called (not staging)

#### Scenario: Unknown role routed to staging
- **GIVEN** a composed ontology that does NOT contain role `slurm_job` (no domain loaded)
- **AND** an Engram observation whose mapped role resolves to `slurm_job`
- **WHEN** the ingester processes the observation
- **THEN** `UpsertEntityStaging(...)` is called

### Requirement: CLI Tooling

`aoc ontology` SHALL provide subcommands usable in downstream CI pipelines.

#### Scenario: Validate in CI
- **WHEN** a CI step runs `aoc ontology validate example.yaml`
- **THEN** it exits 0 on a valid file and non-zero on an invalid file, making it
  usable as a CI gate for domain ontology files.

#### Scenario: Inspect composed ontology
- **WHEN** an operator runs `aoc ontology inspect --group-id example-build`
- **THEN** the command prints all active entity roles and edge types for that group_id

### Requirement: Python Removal

`services/memory/` and `packages/pinard-core/` SHALL be deleted from the pinard
repository once Go parity is validated.

#### Scenario: No Python runtime in the image
- **GIVEN** the updated Dockerfile
- **WHEN** the image is built
- **THEN** no Python interpreter stage is included and all memory functionality is
  provided by Go binaries
