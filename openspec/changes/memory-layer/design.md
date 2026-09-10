## Context

Pinard is a distributed AI agent orchestrator. Workers are currently stateless across sessions — operational knowledge (failure patterns, recovery procedures, threshold decisions) discovered during one run is lost when the session ends. This blocks expansion into long-running data pipeline orchestration (multi-day/week jobs) where agents must learn from human teaching and improve over time.

The Example bioinformatics pipeline is the reference use case: 7 steps, weeks to complete, multiple SLURM jobs, common failure modes (OOM, missing SIF images, shard threshold decisions) that humans currently re-teach on every run.

## Goals / Non-Goals

**Goals:**
- Workers accumulate operational recipes across sessions without direct infrastructure coupling
- Human teaching sessions are captured with high fidelity and extracted into structured knowledge
- Knowledge is scoped per pipeline (`group_id`) — no cross-contamination
- Recipes are version-controlled in git for human curation and auditability
- Workers that crash or restart receive accumulated knowledge at boot
- The ontology evolves: novel patterns emerge from teaching, get reviewed, and become prescribed types

**Non-Goals:**
- Bidirectional sync (git edits back into Graphiti) — v1 is one-way export
- Cross-group knowledge federation (shared SLURM recipes across pipelines)
- Automatic ontology promotion without human review
- Real-time streaming of extraction results to workers mid-session
- Replacing Pinard's event routing or agent lifecycle with Graphiti

## Decisions

> **⚠️ Amended by [`amendment-01-surrealdb.md`](./amendment-01-surrealdb.md).** SurrealDB is now the store of record; Graphiti/FalkorDB becomes a time-boxed evaluation sidecar; Engram is the curated write path; Qdrant is dropped; the ontology is layered. Decisions **1, 8, 12** below are **superseded** (and 4, 5 narrowed) — read the amendment for the authoritative direction. The text below is retained for audit.

### Decision 1: Graphiti (with FalkorDB) as the knowledge graph

> **SUPERSEDED by Amendment 01 (Decisions A1, A2).** SurrealDB = store of record; Graphiti/FalkorDB = eval sidecar only.

**Chosen:** Graphiti temporal knowledge graph backed by FalkorDB.

**Alternatives considered:**
- **Letta** — agent runtime with built-in memory. Rejected: competes with Pinard as an orchestrator. No way to use memory subsystem without adopting the full agent runtime.
- **a commercial SaaS** (evermind.ai) — memory-as-a-service. Rejected: no git export API, opaque extraction logic (black box), limited API surface.
- **Custom extraction + NATS KV** — build our own. Rejected: reinventing temporal validity, dedup, contradiction resolution.

**Why Graphiti:** Developer-controlled ontology via Pydantic models, temporal validity windows on all facts, open source (Apache 2.0), supports FalkorDB as graph backend (lighter than Neo4j), queryable for structured export.

**Why FalkorDB over Neo4j:** Lighter deployment footprint (single binary, Redis-compatible protocol), sufficient for the expected graph size (hundreds to low thousands of facts per pipeline), Graphiti supports it as a first-class backend.

### Decision 2: Episode ingester as a standalone NATS consumer

**Chosen:** A small standalone service (Python) subscribed to `memory.episodes`.

**Alternatives considered:**
- Embedding extraction in the worker extension — rejected: couples workers to Graphiti, violates worker isolation principle.
- Embedding extraction in the conductor — rejected: adds latency to conductor event processing.
- Background thread in the conductor process — rejected: conductor crash loses in-flight episodes.

**Why standalone:** Same pattern as existing Python watchers (`lib/watch_mrs.py`). Independent lifecycle. Can be restarted without affecting workers or conductor. JetStream guarantees delivery even if ingester is temporarily down.

### Decision 3: Cheap model for extraction, expensive model stays on execution

**Chosen:** Haiku/4o-mini for Graphiti extraction, Sonnet for workers, Opus for conductor.

**Rationale:** Extraction runs on every episode (high volume). The task is structured extraction with a schema hint (Pydantic models) — doesn't require top-tier reasoning. Worker/conductor intelligence is unchanged.

### Decision 4: Git as the durable recipe store, NATS request-reply for live queries

**Chosen:** Dual-path — NATS request-reply at boot (fast, live) with git-based fallback (durable, offline).

**Rationale:** NATS request-reply gives workers fresh facts even if the git export hasn't run yet. Git gives durability and human curation. Workers that boot when the ingester is down can still read the last exported snapshot.

### Decision 5: Resilient ingester with LLM availability probing

**Chosen:** The ingester tolerates intermittent LLM availability by distinguishing LLM-unavailable errors from real extraction failures, and retries indefinitely for the former.

**Constraint:** The LLM API token is served via a secret URL (`MEMORY_TOKEN_URL`). The URL always returns a token, but that token is only valid when the user is logged in. When logged out, the token is stale — Anthropic API calls return 401/403. When the user logs back in, the URL serves a refreshed token. The ingester may run for hours without a valid token.

**Design:**
- On startup, fetch token from `MEMORY_TOKEN_URL`, configure Anthropic client, probe with a lightweight API call.
- If probe returns 401/403 (expired token), enter polling sleep.
- Polling sleep: sleep 5 minutes → re-fetch token → re-probe. No message pulling, no NAKing — the ingester is idle. One token URL fetch per 5 minutes maximum.
- When probe succeeds (user logged back in), transition to draining mode: pull messages oldest-first, process sequentially.
- If token expires mid-drain (401/403 during `add_episode`): NAK current message with 5-min delay, immediately transition back to polling sleep. Do NOT continue pulling further messages — this prevents a NAK storm flooding the consumer with redeliveries.
- LLM-unavailable errors (401, 403, connection refused, rate limit) → NAK with escalating backoff (5m → 10m → 30m), retry indefinitely.
- Extraction failures (schema error, malformed response, FalkorDB down) → NAK with short backoff, dead-letter after 5 attempts.
- JetStream retains all episodes durably (7-day retention). No data loss during LLM downtime.
- Status file (`memory-ingester-status.json`) updated on every cycle for dashboard integration.

**Why not queue locally and batch:** JetStream already provides durable queuing with backpressure. Adding a local queue would duplicate state management and create consistency risks.

### Decision 6: /teaching as optional signal, not a gate

**Chosen:** All worker transcripts are always published. Teaching mode just increases extraction aggressiveness.

**Rationale:** If teaching mode were required, forgetting to activate it would mean knowledge loss. Better to always capture (conservatively) and let the human opt in to aggressive extraction when they know they're teaching.

### Decision 7: Pydantic ontology with prescribed/learned/promoted lifecycle

**Chosen:** Start with 8 entity types and 7 edge types. Novel types can emerge in teaching mode and be promoted after human review.

**Rationale:** Avoids the cold-start problem (no ontology = noisy extraction) while allowing the system to grow. The promotion cycle is manual and git-committed — no automatic schema drift.

## Risks / Trade-offs

**[LLM extraction quality]** → Mitigation: Pydantic models as schema hints significantly improve extraction accuracy. Teaching mode (high-context episodes) gives the extraction LLM more signal. Wrong extractions get corrected via contradiction detection on subsequent episodes.

**[FalkorDB operational burden]** → Mitigation: FalkorDB is a single binary, Redis-compatible. Can run as a container alongside the existing NATS deployment. Data volume is small (hundreds of facts per pipeline).

**[Stale recipes injected at boot]** → Mitigation: NATS request-reply returns live Graphiti state. Git fallback is clearly timestamped. Temporal validity windows auto-expire stale facts.

**[Episode volume overwhelming extraction]** → Mitigation: Normal-mode episodes are filtered (tool-only messages skipped). Extraction model is cheap. JetStream provides backpressure — if ingester is slow, messages queue rather than dropping.

**[Ontology too rigid for novel pipelines]** → Mitigation: Learned type emergence in teaching mode. New pipelines start with the base ontology and grow naturally. Proposed types surface in exports for review.

## Migration Plan

1. Deploy FalkorDB alongside existing NATS infrastructure
2. Deploy episode ingester as a new systemd service (or container)
3. Add `memory.episodes` publish hook to worker extension (behind `WORKER_GROUP_ID` env gate — no-op if unset)
4. Add recipe query to worker `session_start` (behind same gate)
5. Add `/teaching` command to conductor
6. Deploy recipe exporter cron
7. Run first Example build with teaching mode to seed the knowledge graph
8. Review exported recipes, promote learned types, iterate ontology

Rollback: Remove `WORKER_GROUP_ID` env var — workers revert to stateless behavior. Ingester and exporter can be stopped independently without affecting agent execution.

### Decision 8: Split write path — per-turn chunks to Qdrant, session-end episodes to Graphiti

> **SUPERSEDED by Amendment 01 (Decisions A3, A5).** Single curated cadence via Engram; per-turn chunk embedding dropped for v1.

**Chosen:** Two write cadences serving different purposes.

**Per-turn (Qdrant):** Each assistant message is published to `memory.chunks`, embedded by a chunk embedder service, and upserted into Qdrant within seconds. Available for mid-session retrieval by other workers immediately.

**Session-end (Graphiti):** The full session transcript is published to `memory.episodes` at session end. The episode ingester extracts entities and edges using the ontology. Requires the full conversational arc to understand causality chains (problem → diagnosis → fix).

**Why split:** A single turn ("the OOM is on shard 47") is useful as a searchable chunk but insufficient for structured extraction. Graphiti needs the complete story. Workers need the chunk *now* — they can't wait for session end.

### Decision 9: Mid-session retrieval via NATS request-reply (memory service abstraction)

**Chosen:** Workers query `memory.recall` via NATS request-reply each turn. A memory service abstracts the retrieval backends.

**Alternatives considered:**
- **Worker-side direct Qdrant HTTP** — rejected: couples workers to Qdrant, can't blend Graphiti results, no server-side intelligence.
- **BTW channel injection from conductor** — rejected: conductor shouldn't be in the retrieval path (adds coupling, single point of failure).

**Why NATS abstraction:** The memory service can blend results from Qdrant (fuzzy semantic) and Graphiti (structured chains), apply relevance gating, summarize using a cheap model, and track session dedup — all invisible to the worker. Backend can be swapped (Qdrant → pgvector → LanceDB) without touching worker code.

**Latency budget:** 3-second timeout. Slow+accurate > fast+inaccurate. If the service is down, workers proceed without context (fail-open).

### Decision 10: Obsidian vault as wiki layer with LLM-powered curation

**Chosen:** Per-vignoble Obsidian vaults (`.wiki/`) plus a global wiki. LLM curator (Sonnet) processes raw inputs into structured pages. Continuous ingest embeds pages into Qdrant.

**Alternatives considered:**
- **Wiki.js or similar hosted wiki** — rejected: requires a web server, overkill for the use case, adds deployment complexity.
- **Plain markdown without tooling** — rejected: no graph view, no structured navigation, no Dataview queries for human review workflows.
- **Notion/Confluence** — rejected: cloud-locked, no local-first, can't be git-tracked.

**Why Obsidian:** It's just markdown files on disk — the curation pipeline writes standard markdown with YAML frontmatter. Obsidian provides the human review layer (graph view for exploring connections, Dataview for finding `needs_review: true` pages, inline editing). Zero runtime dependency — if Obsidian disappears tomorrow, the files are still perfectly readable markdown.

**Scope:** Beyond just Example — covers pinard architecture, operational patterns, incident history, cross-vignoble knowledge. The global wiki is always included in memory service queries.

### Decision 11: Confidence gating for wiki → Qdrant publishing

**Chosen:** Pages with `confidence >= 0.7` are auto-published to Qdrant. Lower confidence pages are stored in the vault but tagged `needs_review: true` and excluded from retrieval until a human approves.

**Rationale:** Prevents low-quality auto-generated content from polluting the knowledge base. Teaching sessions and human-written content auto-publish (high confidence). Single autonomous worker sessions need validation.

### Decision 12: Vector store choice — Qdrant (swappable via memory service abstraction)

> **SUPERSEDED by Amendment 01 (Decisions A1, A4).** Vectors live in SurrealDB; embeddings via Rosetta. The swappable-abstraction intent is preserved.

**Chosen:** Qdrant as the initial vector store. The NATS-mediated memory service ensures this is an implementation detail, not an architectural commitment.

**Why Qdrant initially:** Proven hybrid search (dense + BM25 + RRF), simple deployment (single binary, no JVM), well-documented REST API, metadata filtering for `group_id` scoping, free cloud tier available.

**Why swappable matters:** On HPC, Qdrant runs as a static binary (no Docker needed). But if pgvector (already have Postgres) or LanceDB (embedded, zero infra) prove better fits later, only the memory service internals change. Workers and the wiki pipeline never know.

### Decision 13: Babysitter-first implementation sequencing

**Chosen:** Implement babysitter (data-pipeline spec) before the memory layer. Memory layer follows as Phase 2.

**Rationale:** The first Example babysitter run is a *teaching* run — there's nothing to remember yet. The human is the memory. Babysitter's process-as-code is immediately actionable (SDK exists, Pi adapter exists, need process definition + extension glue). Memory is infrastructure-heavy (multiple new services). The first babysitter run produces high-quality structured input (journal events, gate decisions, task results) that seeds the memory layer when it comes online.

**Interaction:** The memory layer is designed to consume babysitter output as a first-class source. Babysitter journal events are richer than freeform transcripts — typed tasks, structured results, explicit gate decisions. When memory comes online, it can backfill from `.a5c/runs/` journal files from completed runs. No changes to babysitter's architecture are needed to support this — the standard episode publishing and wiki raw feeds handle it.

## Open Questions

- Should the episode ingester batch multiple turns before calling Graphiti, or ingest one-by-one? Batching gives better context but adds latency.
- What's the right `max_facts` limit for boot injection? Too many = context window pressure. Too few = missing critical recipes.
- Should the git export include the full Pydantic model definitions alongside the YAML facts, so the ontology is self-documenting in the repo?
- What embedding model for Qdrant? Qwen3-Embedding-8B (4096d, cheap via OpenRouter) is the Memory OS default. Alternatives: OpenAI text-embedding-3-large (3072d), local models via Ollama.
- Should the global wiki be git-tracked (version control, collaborative editing) or just local files?
- Rate limiting for the curator: 10 docs/hour seems conservative. Profile actual raw input volume to calibrate.
- How should babysitter journal events be mapped to the episode payload format? Option A: journal events published as-is (new `source: "journal"` type). Option B: journal events translated into the standard episode schema with structured `context` metadata. Option C: both — raw journal for Graphiti, translated episodes for Qdrant chunks.
