## Design

### Declarative data model

**Option chosen: declarative JSON-Schema-typed YAML (Option C from the #214 spec).**
Entity/edge definitions are YAML data files, not Python class hierarchies. This
enables static Go builds, native macOS packaging, and runtime extension without
recompilation.

### Two-layer JSON Schema

1. **Meta-schema** (`internal/ontology/meta-schema.json`): validates ontology *files*
   at load time and via `aoc ontology validate`. Ensures domain files are well-formed
   before they reach the registry.

2. **Per-role schema** (`JSONSchemaForRole`): derived at runtime from the composed
   ontology for each entity role. Drives ingester pre-upsert validation and can power
   Graphiti structured-output entity types in future.

### Composition semantics

```
compose(group_id) = core + domain(group_id) − suppressed
```

- **is_a chain**: `mergeInheritedProps` walks up the is_a reference, merging parent
  properties under the child's (domain overrides core on key collision).
- **Edge extension**: domain edges with the same name as core edges extend (append)
  the pairs list. New edge names are added verbatim.
- **Suppressed**: role/edge names in either suppressed list are omitted from the result.
- **Caching**: `Compose` caches per group_id; `LoadFile`/`LoadDirs` invalidate the cache.

### SurrealDB DDL (replaces schema_gen.py)

`GenerateSurrealDDL(composed)` emits DEFINE TABLE / DEFINE FIELD DDL from the
composed ontology. Type map:
- `string` → `string`; `string+format:date-time` → `datetime`
- `integer` → `int`; `number` → `float`; `boolean` → `bool`
- `array` → `array`; `object` → `object FLEXIBLE`
- `minimum`/`maximum` → `ASSERT $value >= N / <= N`
- `enum` → `ASSERT $value INSIDE [...]`

Edge names converted from CamelCase to snake_case for SurrealDB convention.

### Domain file discovery

Runtime loading via `PINARD_ONTOLOGY_DIRS` (colon-separated) and
`<vignoble>/pinard/ontology/*.{yaml,yml,json}`. Missing directories are non-fatal
(logged warning). Invalid files are skipped with a warning; valid siblings still load.

### Ingester integration

`getRegistry()` is a lazy singleton: first call builds the registry from the embedded
core + env-configured domain dirs. `obsToRoleName(content, obsType, groupID, reg)`
replaces the old hardcoded `coreEntityRoles` map — it calls `reg.Compose(groupID).HasRole(role)`
so domain roles (e.g. `slurm_job`) are valid targets if a domain YAML covers the group_id.

### CLI

`aoc ontology validate <file>` — exits 0/non-zero, suitable as a CI gate in domain repos.
`aoc ontology inspect --group-id <gid>` — human-readable view of the composed result.
