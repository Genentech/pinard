## Tasks

### §1 — Core ontology data file
- [x] Write `internal/ontology/core.yaml` — 10 entities, 7 edges, JSON Schema properties

### §2 — Meta-schema
- [x] Write `internal/ontology/meta-schema.json`

### §3 — Go registry
- [x] Write `internal/ontology/ontology.go`
  - [x] `NewRegistry()` — load embedded core.yaml
  - [x] `LoadDirs()` / `LoadFile()` / `LoadFromEnv()`
  - [x] `Compose(group_id)` — merge core + domain − suppressed, with is_a chain
  - [x] `JSONSchemaForRole()` — per-role JSON Schema
  - [x] `GenerateSurrealDDL()` — replace schema_gen.py
  - [x] `ValidateFile()` — meta-schema validation
  - [x] Thread-safe cache

### §4 — Tests
- [x] `internal/ontology/ontology_test.go` — 13 tests covering all public surfaces

### §5 — CLI
- [x] `cmd/aoc/cmd_ontology.go` — `aoc ontology validate` + `aoc ontology inspect`

### §6 — Ingester integration
- [x] Replace hardcoded `coreEntityRoles` with `getRegistry().Compose(groupID).HasRole(role)`
- [x] Add `VIGNOBLE_DIR` and `PINARD_ONTOLOGY_DIRS` env var documentation
- [x] Pass registry through `ingestObservation` / `runEngramIngestion`

### §7 — Reference domain migration (example)
- [x] Write `internal/ontology/testdata/example.yaml` — reference migration of example Python ontology

### §8 — Python deletion
- [x] Delete `services/memory/**`
- [x] Delete `packages/pinard-core/**`
- [x] Update `internal/surreal/schema.surql` comments

### §9 — OpenSpec capability spec
- [x] Write `openspec/changes/memory-ontology-go/proposal.md`
- [x] Write `openspec/changes/memory-ontology-go/design.md`
- [x] Write `openspec/changes/memory-ontology-go/specs/ontology-extension/spec.md`
- [x] Write `openspec/changes/memory-ontology-go/tasks.md`
