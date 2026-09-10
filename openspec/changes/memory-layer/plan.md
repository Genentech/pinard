# Memory Layer — Implementation Plan

> **⚠️ Amended by [`amendment-01-surrealdb.md`](./amendment-01-surrealdb.md).** The dependency graph and Phase 0 below describe the **original** FalkorDB-primary plan. Under the amendment: **SurrealDB** is the store of record (Phase 0), **Rosetta** provides embeddings, **Engram** is the write path, and **Graphiti+FalkorDB** is a time-boxed eval sidecar. The phases below remain useful as reference but defer to the amendment where they conflict.

## Dependency Graph

```
                     [Phase 0: Infrastructure]
                      FalkorDB + NATS stream
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
     [Phase 1a: Ontology]        [Phase 1b: NATS stream setup]
      Pydantic models              pinard-memory stream
                │                           │
                └─────────┬─────────────────┘
                          ▼
              [Phase 2: Episode Ingester]
               Core service + resilience
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
     [Phase 3a:    [Phase 3b:    [Phase 3c:
      Worker        Conductor     Recipe
      hooks]        /teaching]    exporter]
              │           │           │
              └───────────┼───────────┘
                          ▼
              [Phase 4: Observability]
               Status file + dashboard
                          │
                          ▼
              [Phase 5: Example validation]
               End-to-end with real pipeline
```

## External Dependencies

| Dependency | Version | Purpose | Install |
|-----------|---------|---------|---------|
| FalkorDB | 4.x+ | Graph database backend for Graphiti | Docker: `docker run -p 6379:6379 falkordb/falkordb` |
| **SurrealDB** | 2.x | **Store of record** (doc + graph + vector); server + embedded | single static binary (no Docker on HPC) |
| **Rosetta** | dev | **Embeddings** (`qwen3-emb-0.6b`, 1024-d) + entity resolution | `https://dev-rosetta.example.com`, OpenAI-compatible |
| **Engram** | current | **Curated write path** (capture/curate/relation-judgment) | already deployed in the vignoble; cloud-sync → RDS |
| graphiti-core | 0.29+ | Temporal KG SDK — **eval sidecar only** (jsonl-fed) | `pip install graphiti-core` |
| Pydantic | 2.x | Ontology model definitions | Already in graphiti-core deps |
| nats-py | 2.x+ | NATS client for ingester | `pip install nats-py` |
| An LLM endpoint | Haiku/4o-mini | Graphiti extraction (intermittent availability OK) | Via `MEMORY_EXTRACTION_MODEL` env var |
| NATS 2.14+ with JetStream | — | Already deployed for Pinard | Existing infrastructure |

No changes to the worker or conductor's own dependencies (TypeScript/Pi). The new Python dependencies are only for the ingester and exporter services.

## Phase 0: Infrastructure (day 1)

**Goal:** FalkorDB running, NATS stream exists, Graphiti can connect.

**Steps:**

1. **Deploy FalkorDB** alongside existing NATS. Options:
   - Container: `docker run -d --name falkordb -p 6379:6379 -v falkordb-data:/data falkordb/falkordb`
   - On sHPC: Singularity/Apptainer container or bare binary
   - Persistent volume required — graph data must survive restarts

2. **Create NATS stream and consumer.** Can be done via `nats` CLI or programmatically at ingester startup:
   ```bash
   nats stream add pinard-memory \
     --subjects "pinard.*.memory.>" \
     --retention limits --max-age 7d \
     --storage file --replicas 1

   nats consumer add pinard-memory pinard-memory-ingester-default \
     --filter "pinard.default.memory.episodes" \
     --ack explicit --max-deliver -1 \
     --deliver all
   ```
   Note: `--max-deliver -1` means unlimited redelivery (required for LLM-unavailable resilience).

3. **Verify Graphiti ↔ FalkorDB connectivity:**
   ```python
   from graphiti_core import Graphiti
   g = Graphiti("bolt://localhost:6379", "", "")
   # Create and query a test node
   ```

**Deliverable:** FalkorDB reachable, NATS stream exists, Graphiti can read/write a test graph.

**Risk:** FalkorDB uses the Redis wire protocol on port 6379. If Redis is already running (e.g., for Example mappings), use a different port or a separate container network.

## Phase 1: Ontology + Python Foundation (days 2-3)

**Goal:** Ontology models exist and are unit-tested. Python project structure for the ingester is set up.

**Steps:**

1. **Create Python project** for the memory layer services. Suggested location: `services/memory/` (new top-level directory, separate from `lib/` which is legacy Python):
   ```
   services/memory/
     __init__.py
     ontology.py          — Pydantic models (entity types, edge types, edge map)
     ingester.py          — Episode ingester NATS consumer
     exporter.py          — Recipe exporter cron job
     config.py            — Env var parsing, FalkorDB/NATS/LLM config
     requirements.txt     — graphiti-core, nats-py, pyyaml
     tests/
       test_ontology.py
       test_ingester.py
       test_exporter.py
   ```

2. **Implement ontology models** (tasks 2.1–2.4). These are pure Pydantic — no infrastructure needed to develop and test. Start here because everything else depends on the ontology.

3. **Unit test the ontology** (task 2.5): model validation, edge type map completeness (every entity type appears in at least one edge pair), suppressed_types filtering.

**Deliverable:** `ontology.py` with all 8 entity types, 7 edge types, edge type map, and passing unit tests.

**Why first:** The ontology is a pure-code artifact with no infrastructure dependencies. It can be reviewed and iterated before any services are deployed.

## Phase 2: Episode Ingester (days 4-8)

**Goal:** A standalone Python service that consumes episodes from NATS, calls Graphiti, handles LLM unavailability, and writes status.

This is the core of the system and the most complex component. Build it incrementally:

### Step 2a: Basic ingestion loop (tasks 3.1–3.3)

- NATS consumer subscribes to `pinard.{vignoble}.memory.episodes`
- On message: parse payload, call `graphiti.add_episode()` with ontology
- Distinguish normal vs teaching mode (prescribed-only vs learned-enabled)
- **Test with a manually published NATS message** — no worker changes needed yet

### Step 2b: Resilience layer (tasks 3.4–3.7)

- LLM probe on startup
- Error classification: LLM-unavailable (indefinite retry) vs extraction-failure (dead-letter)
- Polling sleep when LLM is down, drain-oldest-first when it comes back
- **Test by starting ingester with no LLM configured**, publishing episodes, then providing the LLM key and watching the drain

### Step 2c: Query handler (task 3.8)

- NATS request-reply on `memory.query`
- Receives `{group_id, max_facts}`, queries Graphiti, returns serialized facts
- **Test with `nats req`** CLI tool

### Step 2d: Observability (tasks 3.9–3.10)

- Status file writer (`memory-ingester-status.json`)
- Structured logging to `memory-ingester.log`

### Step 2e: Integration tests (tasks 3.12–3.13)

- End-to-end: publish episode → verify graph entities
- Resilience: simulate LLM down → verify queue → restore → verify drain

**Deliverable:** `ingester.py` running as a systemd service or long-lived process, with status file visible.

**Deployment:** Run as a systemd user service alongside the existing watchers:
```ini
# ~/.config/systemd/user/pinard-memory-ingester.service
[Service]
ExecStart=/path/to/venv/bin/python services/memory/ingester.py
Environment=PINARD_NATS_URL=127.0.0.1:4222
Environment=FALKORDB_URL=bolt://localhost:6379
Environment=MEMORY_EXTRACTION_MODEL=claude-haiku-4-5-20251001
Restart=always
RestartSec=30
```

## Phase 3: Worker + Conductor + Exporter (days 9-14)

These three are independent of each other — they can be developed in parallel or in any order. They all depend on the ingester being functional.

### Phase 3a: Worker extension changes (tasks 4.1–4.8)

**Files:** `pi-extension/worker/index.ts`

1. Add `WORKER_GROUP_ID` env var reading (task 4.1)
2. Add `group_id` to KV state payload in `session_start` handler (task 4.2)
3. Add episode publish in `message_end` hook — same pattern as existing `btw_reply` publish (tasks 4.3–4.4)
4. Add recipe query in `session_start` — NATS request with timeout, fallback to git file read (tasks 4.5–4.6)
5. Gate everything behind `WORKER_GROUP_ID` being set (task 4.7)

**Key integration point:** The `message_end` hook already exists for btw_reply. The episode publish is an additional publish in the same hook — fire-and-forget to `memory.episodes`, no ack needed from the worker's perspective.

**Testing:** Unit test payload construction. Integration test by running a worker with `WORKER_GROUP_ID=test`, verifying episode appears in NATS stream.

### Phase 3b: Conductor extension changes (tasks 5.1–5.8)

**Files:** `pi-extension/pinard/index.ts`, `pi-extension/pinard/logic.ts`

1. Register `/teaching` command (task 5.1)
2. Implement state toggle (task 5.2)
3. Publish teaching state signal to `memory.teaching.{session}` (task 5.3)
4. Publish user messages as episodes when teaching is active (task 5.4)
5. Implement `--from` and `--all` retroactive capture (tasks 5.5–5.6)
6. Session-end bulk publish (task 5.7)

**Key design choice:** The conductor needs access to its conversation history for `--from` and `--all`. Pi extensions have access to the conversation context — verify the API for retrieving past turns programmatically. If not available, buffer turns in-memory while teaching mode is active.

### Phase 3c: Recipe exporter (tasks 7.1–7.8)

**Files:** `services/memory/exporter.py`

1. Query Graphiti for all current facts per group_id
2. Serialize to YAML (entities, edges, proposed_types)
3. Git diff → commit if changed
4. Set up cron/systemd timer

**This is the simplest component** — a batch script that runs periodically. No long-lived process, no resilience concerns (if it fails, it just runs again next cycle).

**Deployment:** systemd timer or cron:
```bash
# Every 6 hours
0 */6 * * * /path/to/venv/bin/python services/memory/exporter.py
```

## Phase 4: Observability Integration (days 12-14, overlaps with Phase 3)

**Files:** `pi-extension/pinard/index.ts` (conductor TUI)

1. Read `memory-ingester-status.json` in the conductor's TUI refresh cycle (task 6.1)
2. Format and display one-line status (task 6.2)
3. Handle missing/stale file (task 6.3)

**Low risk, low effort.** The conductor already has a TUI dashboard with agent status. Adding a memory status line is a small addition to the existing refresh logic.

## Phase 5: Example Validation (days 15-17)

**Goal:** End-to-end proof that the memory layer works on a real pipeline.

**Prerequisites:** All of Phases 0-4 deployed and working.

**Steps:**

1. Configure Example spawn with `--group example-build` (task 8.1)
2. Connect to a step 5 worker, activate `/teaching` (task 8.2)
3. Teach the OOM recovery recipe through normal conversation
4. Verify the ingester processed it: check `memory-ingester-status.json`, query Graphiti directly (task 8.3)
5. Run the recipe exporter, inspect `recipes/example-build/current.yaml` (task 8.4)
6. Kill the worker, spawn a new step 5 worker, verify recipe injection in boot context (task 8.5)
7. Simulate an OOM condition, verify the worker applies the recipe without human help (task 8.6)

**This is the acceptance test.** If this works, the memory layer delivers value.

## Rollback Strategy

Each phase can be rolled back independently:

| Phase | Rollback |
|-------|----------|
| Infrastructure | Stop FalkorDB container. Remove NATS stream: `nats stream rm pinard-memory` |
| Ontology | Delete `services/memory/`. No runtime impact |
| Ingester | Stop systemd service. Episodes queue in NATS (7-day retention) but aren't processed |
| Worker hooks | Remove `WORKER_GROUP_ID` from spawn env. All memory hooks become no-ops |
| Conductor /teaching | Remove command registration. No functional impact on existing commands |
| Exporter | Stop cron. Existing recipes in git remain but aren't updated |
| Dashboard | Remove status line read. Dashboard shows everything else normally |

**Total rollback:** Unset `WORKER_GROUP_ID` env var in spawn config. Workers revert to fully stateless behavior. No data loss (episodes remain in NATS, graph remains in FalkorDB).

## Resolved Decisions

1. **FalkorDB hosting:** EKS deployment alongside NATS. Separate pod/service, same cluster.

2. **Python project location:** `services/memory/` (new top-level directory).

3. **LLM key propagation:** Token served via a secret URL (configurable). The ingester curls this URL to obtain the API key. When the user is logged out, the URL returns 401/403 (token expired). When logged back in, the URL returns a fresh token. The ingester's resilience loop handles this:
   - On startup and periodically: `curl $MEMORY_TOKEN_URL`
   - If 200: extract token, configure Anthropic client, mark `llm_available = true`
   - If 401/403: mark `llm_available = false`, enter polling sleep, retry every 5 minutes
   - The token URL itself is a secret — configured via `MEMORY_TOKEN_URL` env var, never logged

4. **`--group` flag:** Add to `aoc spawn`, sets `WORKER_GROUP_ID` env var in worker environment.

## Timeline Estimate

| Phase | Duration | Can parallel with |
|-------|----------|-------------------|
| 0: Infrastructure | 1 day | — |
| 1: Ontology | 2 days | — |
| 2: Ingester | 5 days | — |
| 3a: Worker hooks | 3 days | 3b, 3c |
| 3b: Conductor /teaching | 3 days | 3a, 3c |
| 3c: Recipe exporter | 2 days | 3a, 3b |
| 4: Dashboard | 1 day | 3a, 3b, 3c |
| 5: Example validation | 3 days | — |
| **Total** | **~17 working days** | (with parallelism in Phase 3: ~14 days) |
