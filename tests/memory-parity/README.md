# Memory parity — Python → Go regression guard

This directory holds the fixture corpus, Python golden baseline, and runbooks for
memory backend parity testing. It guards against silent regressions in the Go memory
backend (`cmd/memory-ingester`, `cmd/memory-recall`, `internal/surreal`,
`internal/ontology`) relative to the deleted Python implementation.

**Background:** MR !431 / issue #214 replaced the Python memory services
(`services/memory/`) with Go binaries and deleted the Python test suite. The Go
unit tests in `internal/surreal/`, `internal/memory/`, `internal/ontology/`, and
`cmd/memory-*/` port the behavioral contracts from the deleted Python tests. This
directory holds the captured Python golden baseline and runbooks for data-compatibility
checks that require a live SurrealDB/NATS environment.

## Python golden baseline (captured from master before !431 merged)

The `golden/` directory was captured by running the Python `pinard-core` library
(installed from master) and `services/memory/surrealdb/schema_gen.py` against the
fixture corpus **before** !431 deleted the Python services:

| File | Source | Description |
|------|--------|-------------|
| `golden/ontology.json` | `pinard-core` (master) | Entity roles, edge table names, obs_type→role mapping |
| `golden/schema.sql` | `schema_gen.generate_schema_ddl()` (master) | Full SurrealDB DDL as Python would produce it |
| `golden/recall.json` | Python `_observation_to_entity()` (master) | Per-corpus entity role/name predictions |

The `memory-integration` CI job asserts Go parity against these golden files:
- All 7 Python edge table names queryable in Go schema
- All entity fields present in Go schema
- `obs_type_to_role` map identical between Python and Go
- All 8 corpus entities upsertable with correct roles

If any of these fail, it means the Go backend would silently orphan existing
production memory data or produce wrong entity classifications.

---

## Fixture corpus

`corpus/observations.json` — 8 curated observations covering all canonical
`obsTypeToRole` mappings (`rule`, `fact`, `diagnosis`, `teaching-episode`,
`decision`, `action`, `environment_condition`, `log_pattern`). Used by:

- The `memory-integration` CI job (`-tags integration` tests, when written).
- Manual data-compat and shadow-run steps below.

---

## A. Data-compatibility test (manual)

**Goal:** verify the Go recall service can query a SurrealDB database that was
populated by the Python ingester (no schema-drift orphaning production memory).

Prerequisites:
- A SurrealDB dump written by the Python ingester (e.g. exported before !431 merged).
- A running Go `memory-recall` binary pointed at that dump.
- `SURREAL_URL`, `SURREAL_USER`, `SURREAL_PASS`, `NATS_URL`, `NATS_VIGNOBLE` set.

Steps:

```bash
# 1. Restore the Python-written SurrealDB dump into a test database.
surreal import --conn $SURREAL_URL --user $SURREAL_USER --pass $SURREAL_PASS \
  --ns pinard --db memory-parity-compat <python-dump.surql>

# 2. Start the Go recall service against the restored database.
NATS_VIGNOBLE=memory-parity-compat \
SURREAL_URL=$SURREAL_URL SURREAL_USER=$SURREAL_USER SURREAL_PASS=$SURREAL_PASS \
  memory-recall &

# 3. Send a recall request for each query in the fixture set.
for query in "JetStream durable events" "SurrealDB HNSW" "ingester stalls" \
             "reset ingest cursor" "Go memory backend" "EnsureSchema"; do
  nats request "pinard.memory-parity-compat.recall" \
    "{\"group_id\":\"memory-parity-compat\",\"query\":{\"user_message\":\"$query\"}}" \
    --timeout 5s
done

# 4. Assert: context is non-empty and sources contain expected entity names.
# Pass criterion: all 6 queries return at least 1 source matching the fixture corpus.
```

Expected result: Go recall returns memories from the Python-written database without
errors. Any `"surreal: "` errors indicate schema drift between Python and Go schemas.

---

## B. Shadow run (highest confidence, pre-cutover)

**Goal:** run Go ingester in parallel with Python on the same Engram stream but a
separate `group_id`, then diff the resulting graphs.

```bash
# 1. In a live vignoble (e.g. misc), start Go ingester against a shadow group_id.
NATS_VIGNOBLE=misc \
MEMORY_GROUP_IDS=misc-go-shadow \
ENGRAM_URL=http://localhost:7783 \
SURREAL_URL=http://localhost:8000 SURREAL_USER=root SURREAL_PASS=<pass> \
  memory-ingester &

# 2. Let it run for a period (≥1 ingestion cycle = 30 min) alongside the Python ingester.

# 3. Compare entity counts and roles:
surreal sql --conn $SURREAL_URL --user root --pass <pass> \
  --ns pinard --db misc-go-shadow \
  "SELECT role, count() FROM entity GROUP BY role"

surreal sql --conn $SURREAL_URL --user root --pass <pass> \
  --ns pinard --db misc \
  "SELECT role, count() FROM entity GROUP BY role"

# 4. Compare recall quality:
nats request "pinard.misc.recall" \
  '{"group_id":"misc","query":{"user_message":"recent decisions"}}' --timeout 5s
nats request "pinard.misc.recall" \
  '{"group_id":"misc-go-shadow","query":{"user_message":"recent decisions"}}' --timeout 5s

# Pass criterion: entity role distribution matches within ±10%; recall top-k overlap ≥90%.
```

Cut over only when parity holds.

---

## C. Recall@k parity queries (fixture set)

Fixed queries to run against both Python and Go backends. Use these when comparing
recall quality between implementations.

| # | Query | Expected top entity (role) |
|---|-------|--------------------------|
| 1 | "JetStream durable events" | rule → decision |
| 2 | "SurrealDB vector search HNSW" | fact → artifact |
| 3 | "ingester timeout stalls" | diagnosis → diagnosis |
| 4 | "reset ingest cursor reingest" | teaching-episode → task |
| 5 | "Go memory backend canonical" | decision → decision |
| 6 | "apply schema before ingesting" | action → action |
| 7 | "SurrealDB version requirement" | environment_condition → environment_condition |
| 8 | "ENGRAM_URL connection refused error" | log_pattern → log_pattern |

**Threshold:** recall@8 overlap ≥ 90% between Python and Go (i.e. at least 7 of 8
queries must return the expected entity in the top-k results).

---

## D. Cleanup

After manual runs, drop the test databases:

```bash
surreal sql --conn $SURREAL_URL --user root --pass <pass> \
  --ns pinard "REMOVE DATABASE IF EXISTS `memory-parity-compat`"
surreal sql --conn $SURREAL_URL --user root --pass <pass> \
  --ns pinard "REMOVE DATABASE IF EXISTS `memory-parity-test`"
surreal sql --conn $SURREAL_URL --user root --pass <pass> \
  --ns pinard "REMOVE DATABASE IF EXISTS `misc-go-shadow`"
```
