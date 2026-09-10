> **⚠️ Amended by [`amendment-01-surrealdb.md`](./amendment-01-surrealdb.md).** §1, §2, §3, §7 are revised below; §10 and §11 are new. Unmarked task groups (§4–§6, §8–§9) still apply, adjusted only where they name the old stack.

## 1. Infrastructure Setup (amended)

- [ ] 1.1 Deploy **SurrealDB** (single static binary; server mode for the central store, embedded/file mode for portable subsets) alongside existing NATS
- [ ] 1.2 Define SurrealDB namespace/database scoping per `group_id` (vigne); define schema for entities, edges (`RELATE`), vectors (HNSW, dim=1024), and wiki documents
- [ ] 1.3 Stand up the **Rosetta embedding client** (`/api/v1/embeddings`, model `qwen3-emb-0.6b`, 1024-d); verify write/query embedding round-trip
- [ ] 1.4 Create `pinard-memory` JetStream stream covering `pinard.{vignoble}.memory.>` (7-day retention) + durable consumer `pinard-memory-ingester-{vignoble}`
- [ ] 1.5 Stand up the **eval-only Graphiti+FalkorDB sidecar** (time-boxed); verify Graphiti creates/queries a test graph (feeds the Phase-2 KG decision)

## 2. Ontology Registry (amended — layered)

- [ ] 2.1 Create the **`pinard-core`** base ontology (Pydantic): `Task`/`Step`, `Verdict`/`Decision`, `Gate`, `Action`, `Diagnosis`, `LogPattern`, `EnvironmentCondition`, `Artifact`
- [ ] 2.2 Create the base edge models (`DependsOn`, `Produces`, `Consumes`, `IndicatesProblem`, `ResolvedBy`, `RequiresCondition`, `TriggersDecision`) + the edge-type map (valid source→target pairs)
- [ ] 2.3 Implement the **layering/registry mechanism**: per-repo domain ontologies subclass core; registry composes `core + active domain` per `group_id` (Graphiti per-call `entity_types`/`edge_types`; SurrealDB relation names)
- [ ] 2.4 Add ontology **versioning** (core + domain versions) and `suppressed_types`; define the migration policy for shipped subsets
- [ ] 2.5 Write unit tests: base model validation, edge-type map, and core+domain composition

## 3. Curated Ingester Service (amended — Engram → SurrealDB)

> Was "Episode Ingester" (raw transcripts → Graphiti). Now pulls **curated Engram observations** and writes to SurrealDB. Per-turn chunk embedding is dropped for v1 (Decision A5).

- [ ] 3.0 Pull curated observations from Engram (read API preferred; RDS fallback — resolved by Spike 1)
- [ ] 3.1 Embed via Rosetta and upsert records + vectors into SurrealDB; map observations to composed `core+domain` ontology entities/edges
- [ ] 3.1b Create episode ingester Python service with NATS consumer subscription to `memory.episodes` (for `/teaching`-episode extraction)
- [ ] 3.2 Implement normal-mode ingestion: call `graphiti.add_episode()` with prescribed types only
- [ ] 3.3 Implement teaching-mode ingestion: enable learned type emergence, lower confidence thresholds
- [ ] 3.4 Implement token fetch from `MEMORY_TOKEN_URL` on startup, configure Anthropic client, probe API to verify validity (polling sleep if 401/403 from Anthropic)
- [ ] 3.5 Implement LLM-unavailable retry: on 401/403 from Anthropic, re-fetch token from `MEMORY_TOKEN_URL`, reconfigure client, escalating backoff (5m → 10m → 30m), retry indefinitely
- [ ] 3.6 Implement extraction-failure retry: short backoff (10s → 30s → 60s), dead-letter after 5 attempts to `memory.dead`
- [ ] 3.7 Implement backlog drain: process oldest-first when LLM becomes available, pause on LLM loss
- [ ] 3.8 Implement NATS request-reply handler on `memory.query` for worker boot queries
- [ ] 3.9 Implement status file writer: update `{vignoble}/logs/memory-ingester-status.json` on every cycle (state, llm_available, queue_depth, last_extraction_at, extracted_today, errors_today, oldest_pending_age_hours)
- [ ] 3.10 Add ingestion logging to `{vignoble}/logs/memory-ingester.log`: session_id, group_id, mode, entities/edges extracted, duration_ms
- [ ] 3.11 Configure extraction model via `MEMORY_EXTRACTION_MODEL` env var (default Haiku)
- [ ] 3.12 Write integration test: publish episode to NATS → verify entities created in Graphiti
- [ ] 3.13 Write integration test: simulate LLM unavailability → verify episodes queue → restore LLM → verify drain

## 4. Worker Extension Changes

- [ ] 4.1 Add `WORKER_GROUP_ID` env var support to worker extension
- [ ] 4.2 Add `group_id` field to KV state publishing in `session_start`
- [ ] 4.3 Add `message_end` hook: publish episode payload to `pinard.{vignoble}.memory.episodes` when text content present
- [ ] 4.4 Skip episode publish for tool-only responses (no text content)
- [ ] 4.5 Add `session_start` hook: send NATS request to `memory.query` with group_id, inject response via `sendUserMessage` with `deliverAs: "steer"`
- [ ] 4.6 Implement 5-second timeout on recipe query with git-based fallback (`recipes/{group_id}/current.yaml`)
- [ ] 4.7 Gate all memory features behind `WORKER_GROUP_ID` being set (no-op if unset)
- [ ] 4.8 Write unit tests for episode payload construction and message_end filtering

## 5. Conductor Extension Changes

- [ ] 5.1 Register `/teaching` command with arguments: `on`, `off`, `stop`, `--from <duration>`, `--all`
- [ ] 5.2 Implement `teachingMode` flag toggle on activation/deactivation
- [ ] 5.3 Publish teaching state to `pinard.{vignoble}.memory.teaching.{session}` on toggle
- [ ] 5.4 Publish user messages as episodes with `source: "conductor"`, `mode: "teaching"` when active
- [ ] 5.5 Implement `--from <duration>` retroactive capture: retrieve past turns, publish as single teaching episode
- [ ] 5.6 Implement `--all` full session capture
- [ ] 5.7 Implement session-end bulk publish when teaching was active during session
- [ ] 5.8 Write unit tests for teaching mode state transitions

## 6. Conductor Dashboard Integration

- [ ] 6.1 Read `{vignoble}/logs/memory-ingester-status.json` in conductor TUI refresh cycle
- [ ] 6.2 Display one-line memory status: queue depth, last extraction time, LLM state
- [ ] 6.3 Handle missing/stale status file gracefully (show "Memory: status unknown")

## 7. Exporters (amended — jsonl-for-Graphiti + portable subset)

> The git-YAML recipe export is superseded by (a) `SurrealDB → jsonl → Graphiti` eval export and (b) the portable-subset export (§10). Tasks below are retained where still useful.

- [ ] 7.0 Implement `SurrealDB → jsonl` export (nodes.jsonl, edges.jsonl) feeding the Graphiti eval sidecar
- [ ] 7.1 Create recipe exporter script (Python) that queries Graphiti for all current facts per group_id
- [ ] 7.2 Implement YAML serialization: entities, edges, proposed_types sections
- [ ] 7.3 Implement git commit strategy: stage `recipes/` changes, commit with `[memory-export]` prefix, no auto-push
- [ ] 7.4 Implement no-op detection: skip commit when output unchanged
- [ ] 7.5 Implement multi-group export in a single run
- [ ] 7.6 Add export logging to `{vignoble}/logs/memory-export.log`: groups processed, facts per group, commit SHA, duration
- [ ] 7.7 Set up cron/systemd timer (default 6-hour interval, configurable via `MEMORY_EXPORT_INTERVAL`)
- [ ] 7.8 Write integration test: seed Graphiti with test facts → run exporter → verify YAML output

## 8. Example Validation

- [ ] 8.1 Configure Example pipeline with `group_id: example-build`
- [ ] 8.2 Run a teaching session on step 5 (optimize): teach OOM recovery recipe
- [ ] 8.3 Verify Graphiti extracts LogPattern → Diagnosis → Action chain
- [ ] 8.4 Run recipe exporter, verify `recipes/example-build/current.yaml` contains the recipe
- [ ] 8.5 Boot a new step 5 worker, verify recipe is injected at startup
- [ ] 8.6 Verify the worker handles a simulated OOM using the injected recipe without human guidance

## 9. Documentation

- [ ] 9.1 Add memory layer setup instructions to `docs/` (FalkorDB, ingester, exporter deployment)
- [ ] 9.2 Document `/teaching` command usage in conductor docs
- [ ] 9.3 Document `WORKER_GROUP_ID` and `MEMORY_EXTRACTION_MODEL` env vars
- [ ] 9.4 Document ontology evolution workflow (review proposed_types → promote or suppress)

## 10. Portable Memory Subset (new)

- [ ] 10.1 Implement subset/export: select central SurrealDB by scope (namespace/DB or `group_id`) into a fresh **embedded** SurrealDB file
- [ ] 10.2 Version-stamp the subset with `pinard-core` + domain ontology versions (reproducibility/audit)
- [ ] 10.3 Load the embedded subset locally at agent boot (replaces git-YAML fallback in boot-injection)
- [ ] 10.4 Integrate with the versioning target (e.g. ExoHub): `harness + babysitter process + memory` bundle
- [ ] 10.5 Integration test: subset a scope → load embedded → recall matches central store

## 11. Scope Roll-up & Promotion (new)

- [ ] 11.1 Compute vignoble/global roll-ups from project-scoped facts using `vignes.yaml` membership
- [ ] 11.2 Detect recurrence/contradiction candidates via Engram relation judgment (`related`/`scoped`/`supersedes`/`conflicts_with`)
- [ ] 11.3 Surface promotion candidates (rules and ontology domain→core) as Obsidian checkboxes/forms
- [ ] 11.4 Implement the Obsidian form → git PR bridge for human-gated promotion
- [ ] 11.5 Integration test: same rule across N vignobles → candidate → approve → PR
