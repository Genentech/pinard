# Memory KG Evaluation — Phase-2 Decision Input

**Status:** tooling complete, end-to-end eval pending
**Amendment ref:** `openspec/changes/memory-layer/amendment-01-surrealdb.md` §Decision A2
**Issue:** [#44](https://gitlab.example.com/your-group/pinard/-/work_items/44)

> **Note on evidence:** This spike delivered the eval pipeline (export → ingest → assess) and unit tests (all mocked). An end-to-end run against live services (populated SurrealDB + FalkorDB + Anthropic LLM) has **not yet executed** — the `entity` table requires the #32 ingester (not yet merged), and Graphiti NER requires a live `MEMORY_TOKEN_URL`. The sections below distinguish **what ran** from **design-level estimates and projections**.

---

## Purpose

Build the tooling that enables the time-boxed evaluation defined in amendment-01
Decision A2: run real SurrealDB data through the Graphiti+FalkorDB eval sidecar,
assess temporal-KG capabilities, and inform the Phase-2 decision.

The data pipeline:

```
SurrealDB (central store)
  └─ export.py → nodes.jsonl + edges.jsonl
        └─ ingest.py → Graphiti (FalkorDB)
              └─ assess.py → search probes → findings
```

---

## What was built and tested

| Module | Purpose | Status |
|--------|---------|--------|
| `services/memory/graphiti_eval/export.py` | Queries all `entity` records and RELATE edges from SurrealDB; writes NDJSON (`nodes.jsonl`, `edges.jsonl`). CLI: `python -m services.memory.graphiti_eval.export --group-id <id>` | ✅ unit-tested (mocked SurrealClient) |
| `services/memory/graphiti_eval/ingest.py` | Reads nodes/edges NDJSON and feeds Graphiti as episodes (one episode per node, outgoing edges appended for NER context). CLI: `python -m services.memory.graphiti_eval.ingest --group-id <id>` | ✅ unit-tested (mocked Graphiti) |
| `services/memory/graphiti_eval/assess.py` | Runs 7 representative search probes, tracks cross-probe fact deduplication, emits NDJSON results. CLI: `python -m services.memory.graphiti_eval.assess --group-id <id>` | ✅ unit-tested (mocked Graphiti) |
| `services/memory/tests/test_graphiti_eval.py` | 21 unit tests total (15 new): TestSurrealExport, TestGraphitiIngest, TestGraphitiAssessment + pre-existing Graphiti client/verify tests | ✅ 21/21 pass |

**What has not run:** an end-to-end pipeline against live services. Prerequisites:
- The `entity` table must be populated (requires #32 ingester or manual seeding).
- FalkorDB must be reachable at `FALKORDB_URL`.
- Anthropic API key must be available (`ANTHROPIC_API_KEY` or `MEMORY_TOKEN_URL`).

---

## Assessment dimensions (design analysis, not measured)

The following is a **design-level analysis** of what Graphiti provides and what
reproducing it natively in SurrealDB would entail. Numbers are estimates, not
measurements from a live run.

### 1. Temporal validity / contradiction resolution

Graphiti maintains bi-temporal validity windows on extracted facts (`valid_at`,
`invalid_at`). When the same entity appears in two episodes with contradictory
property values, Graphiti is designed to invalidate the earlier fact and preserve
both in the timeline.

**Effort to reproduce natively in SurrealDB (estimate):** Medium–High.
SurrealDB has no built-in bi-temporal indexing. Reproducing this requires:
- Two timestamp columns per entity fact (`valid_from`, `valid_until`)
- A write path that closes the previous window on upsert (transaction + sub-query)
- Query-time validity filtering
- Contradiction detection (LLM or rule-based)

Estimated effort: **4–6 weeks** for a robust implementation, not including testing.

### 2. NER / entity-edge extraction quality

Graphiti's `add_episode` pipeline passes episode text through an Anthropic LLM
and extracts typed entity nodes and typed edges using the supplied `entity_types` /
`edge_types` lists.

**Episode content strategy:** each node becomes one episode; outgoing edges are
appended to the episode body so the LLM sees relational context. This is the
strategy implemented in `ingest.py` — whether it produces good recall in practice
requires a live run to measure.

**Effort to reproduce natively in SurrealDB (estimate):** High.
The LLM NER pipeline is the core of Graphiti's value. Reproducing it requires:
- A structured extraction prompt + output schema
- Entity deduplication / resolution logic
- Confidence-gated insertion
- Retry/backoff on LLM failures

Estimated effort: **6–10 weeks** for extraction quality comparable to Graphiti.

### 3. Phase-2 options (decision belongs to the maintainer)

The amendment-01 framing is explicit: Graphiti is a **time-boxed eval sidecar**.
The Phase-2 decision — once the eval has actually run — picks one of:

| Option | Pros | Cons | Effort |
|--------|------|------|--------|
| **(i) Keep Graphiti+FalkorDB permanent** | Production-ready temporal KG + NER today; zero rebuild cost | Partial consolidation (two stores); FalkorDB ops overhead; LLM token dependency for all writes | Low (already built) |
| **(ii) Native SurrealDB temporal KG** | Full consolidation; no FalkorDB; SurrealDB already in-stack | ~10–16 weeks of core infrastructure rebuild (estimates above); NER quality unproven | Very high |
| **(iii) Spectron (pending OSS)** | Graphiti-like temporal KG on SurrealDB; no store migration if/when released | Not yet OSS; timeline unknown | Unknown |

The effort estimates above are the primary quantitative input from this spike.
The **recommendation** (which option to adopt) requires measured recall/dedup
data from a live eval run and is the maintainer's call.

---

## Running the eval (prerequisites)

```bash
# Prerequisites:
# 1. SurrealDB running with populated entity table (requires #32 ingester or manual seed)
# 2. FalkorDB running (FALKORDB_URL=bolt://localhost:6379)
# 3. Anthropic API key (ANTHROPIC_API_KEY or MEMORY_TOKEN_URL)

# Export from SurrealDB
python -m services.memory.graphiti_eval.export \
    --group-id <your-group-id> \
    --nodes-out /tmp/nodes.jsonl \
    --edges-out /tmp/edges.jsonl

# Ingest into Graphiti
python -m services.memory.graphiti_eval.ingest \
    --group-id <your-group-id> \
    --nodes /tmp/nodes.jsonl \
    --edges /tmp/edges.jsonl

# Run assessment probes (results as NDJSON to stdout)
python -m services.memory.graphiti_eval.assess \
    --group-id <your-group-id> \
    --limit 5 \
  | jq .

# Unit tests (no live services required — all mocked)
python3.9 -m pytest services/memory/tests/test_graphiti_eval.py -v -k "not integration"
# → 21 passed

# Integration test (requires live FalkorDB + Anthropic key)
python3.9 -m pytest -m integration services/memory/tests/test_graphiti_eval.py -v
```

Environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SURREAL_URL` | `http://localhost:8000` | SurrealDB endpoint |
| `SURREAL_USER` | `root` | SurrealDB username |
| `SURREAL_PASS` | _(required)_ | SurrealDB password |
| `FALKORDB_URL` | `bolt://localhost:6379` | FalkorDB bolt URL |
| `ANTHROPIC_API_KEY` | _(or `MEMORY_TOKEN_URL`)_ | Anthropic key for NER |
| `MEMORY_EXTRACTION_MODEL` | `claude-haiku-4-5-20251001` | LLM for Graphiti NER |

---

## Open questions (to answer in the live eval)

- **Dedup rate:** what fraction of facts does Graphiti collapse as duplicates across repeated ingestion of the same node?
- **NER recall by entity type:** which roles (`task`, `log_pattern`, `environment_condition`, `gate`, …) are reliably extracted vs. missed?
- **Episode length sensitivity:** at what episode token length does extraction quality degrade?
- **Incoming-edge coverage:** does appending only outgoing edges miss entity types that primarily appear as edge targets?
- **FalkorDB persistence:** is the Graphiti store treated as ephemeral (re-derived from SurrealDB on demand) or durable?
- **Write latency impact:** if Graphiti is retained permanently, what is the P95 latency cost of dual-store writes?
- **Spectron timeline:** when does it reach OSS / production readiness?
