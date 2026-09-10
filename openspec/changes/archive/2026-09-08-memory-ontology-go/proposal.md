## Why

The Go memory binaries (memory-ingester, memory-recall, memory-rollup) introduced in
#214 had a hardcoded placeholder (`coreEntityRoles`) instead of a real ontology
registry. Domain extensions (e.g. example) could not be registered without code
changes. This change completes the ontology layer, eliminating the last Python
dependency (`packages/pinard-core`, `services/memory/`) and enabling downstream
vigne owners to extend the ontology purely with a YAML file — no pinard rebuild.

## What Changes

- **Declarative core ontology** (`internal/ontology/core.yaml`, embedded): 10 entity
  roles / 7 edge types encoded as YAML with literal JSON Schema property definitions.
  Replaces the Python Pydantic subclass model in `packages/pinard-core/`.
- **Go ontology registry** (`internal/ontology/ontology.go`): `NewRegistry()` loads
  the embedded core; `LoadFromEnv(vignoble)` scans `PINARD_ONTOLOGY_DIRS` and
  `<vignoble>/pinard/ontology/*.yaml` for domain extensions; `Compose(group_id)` →
  `ComposedOntology` with merged entity/edge maps. Thread-safe, cached.
- **Meta-schema** (`internal/ontology/meta-schema.json`, embedded): validates
  ontology files at load time and via `aoc ontology validate <file>` (CI-ready).
- **SurrealDB DDL generation** (`ontology.GenerateSurrealDDL`): derives DEFINE TABLE
  / DEFINE FIELD DDL from the composed ontology — replaces `services/memory/surrealdb/schema_gen.py`.
- **JSON Schema per role** (`ontology.JSONSchemaForRole`): per-role schema derived
  from entity definitions, ready for pre-upsert record validation.
- **CLI** (`aoc ontology validate <file>`, `aoc ontology inspect --group-id`): lets
  vigne owners validate their domain YAML in CI and inspect the composed result.
- **Reference domain migration** (`internal/ontology/testdata/example.yaml`): the
  example Python entities/edges/registry ported to the declarative format, proving the
  extension mechanism. Copy to `example-workers/pinard/ontology/example.yaml` + set
  `PINARD_ONTOLOGY_DIRS` to activate.
- **Ingester uses registry**: `cmd/memory-ingester` replaced the hardcoded
  `coreEntityRoles` map with `getRegistry().Compose(groupID).HasRole(role)`.
- **Delete Python**: `services/memory/**` and `packages/pinard-core/**` removed.

## Capabilities

### New Capabilities
- `ontology-extension`: The declarative ontology extension contract — YAML envelope
  format, is_a chain composition, runtime domain loader, meta-schema validation,
  SurrealDB DDL generation, and CLI tooling. Downstream vigne owners extend the
  ontology with a single YAML file; no pinard rebuild required.
