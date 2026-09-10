# Amendment 01 — SurrealDB Consolidation, Engram Write-Path, Layered Ontology

**Status:** proposed · **Supersedes:** design.md Decisions 1, 8, 12 (and narrows 4, 5) · **Applies to:** the `memory-layer` change (not yet implemented)

This amendment records a deliberate pivot of the memory-layer architecture. The original change is preserved for audit; the decisions below are **authoritative** where they conflict with the original `design.md`/`proposal.md`. Per-capability `specs/*/spec.md` scenario deltas are reconciled as tracked tasks (see §Tasks Impact).

## Why the pivot

Three drivers, in priority order:

1. **Technology consolidation.** The original stack spanned FalkorDB + Qdrant + git-YAML + Obsidian + Graphiti. SurrealDB is multi-model (document + graph via `RELATE` + native vector HNSW) and runs **either** as a server **or** as an embedded, file-based engine. That duality unlocks the next driver.
2. **Memory as a versioned, portable artifact.** A central SurrealDB (server) is the memory service where all agents write. It can be **subset** — scoped to a repo/pipeline — into a specialized *embedded* SurrealDB file that a pinard agent loads locally. A "pinard agent" becomes `harness + babysitter process + memory`, all three versioned (e.g. in ExoHub) for **reproducible, auditable** agent-centric data pipelines.
3. **Signal over noise.** The original design sent *all* transcripts to Graphiti and relied on LLM extraction to find signal (its own #1 stated risk). Engram already produces curated, typed, scoped observations. Using Engram as the write path removes most extraction noise.

## New / superseding decisions

### Decision A1 — SurrealDB as store of record (supersedes Decision 1 storage, Decision 12)

**Chosen:** SurrealDB is the single store of record for structured facts, vectors, and wiki documents. It replaces FalkorDB (as the primary graph store) and Qdrant (as the vector store).

**Why:** Multi-model in one engine (document + graph + vector), single static binary (fits the HPC "no Docker" constraint), server-or-embedded duality (enables portable subsets), native `RELATE` graph traversal, HNSW/MTREE vector indexes, per-`group_id` isolation via namespaces/databases.

**Backend abstraction preserved:** Workers never touch SurrealDB directly. All access is through the memory service's typed intent API (Decision A5). This keeps Decision 12's core intent — the store is an implementation detail — while changing the chosen implementation.

### Decision A2 — Graphiti as a time-boxed KG evaluation sidecar (supersedes Decision 1 engine role)

**Chosen:** SurrealDB is the store of record; **Graphiti (on FalkorDB) is a downstream evaluation sidecar**, fed via JSON Lines export (`SurrealDB → jsonl → Graphiti`). It is used to explore temporal-KG value and NER quality on real data **before** committing to a permanent KG engine.

**Why not native temporal-KG-in-SurrealDB now:** Graphiti provides two hard things cheaply — bi-temporal validity/contradiction resolution **and** an LLM NER/entity-edge extraction pipeline. Rebuilding both natively is large and uncertain. Evaluate first, then decide.

**Data flow:** one-way, `SurrealDB → Graphiti` (analysis projection). No bidirectional sync in v1.

**Explicitly time-boxed:** this reintroduces FalkorDB as an eval-only sidecar. It is **not** a permanent two-store architecture. A **Phase-2 decision** picks one of:
- (i) keep Graphiti+FalkorDB permanent (accept partial consolidation),
- (ii) build native temporal-KG in SurrealDB (informed by the evaluation),
- (iii) adopt **Spectron** (upcoming, not-yet-OSS; Graphiti-like temporal KG *on* SurrealDB) once viable — the preferred long-term slot, requiring no store migration.

### Decision A3 — Engram as the curated write path (supersedes Decision 8 write path)

**Chosen:** Curated knowledge enters via **Engram**, not raw-transcript episodes. Two explicit inputs plus a passive safety net:
- **`/lesson <text>`** — a one-shot pinned fact/rule (type `rule`/`fact`), high-confidence, eligible for promotion. E.g. `/lesson always use fix: or feat: commit prefix`.
- **`/teaching`** — a *mode*; while active, the session is captured as a `teaching-episode` from which the backend extracts a runbook/procedure/ontology edges (higher aggressiveness).
- **Passive capture** (Engram session summaries) — safety net so lessons are not purely manual (preserves original Decision 6's "always capture" intent).

**Teaching mode lifecycle (resolves the original's missing disable path):** explicit off (`/teaching off` or re-toggle) **and** a visible banner while active **and** auto-off at session end (session-scoped; never persists silently).

**Sync path:** Engram cloud sync → Postgres RDS (or Engram read API). The memory service pulls curated observations from there, embeds (Decision A4), maps to the ontology (Decision A6), and writes to SurrealDB.

**Naming note:** `/teaching`-captured content and `/lesson` pins both become "lessons" conceptually but carry different observation types (`teaching-episode` vs `rule`/`fact`).

### Decision A4 — Rosetta for embeddings (resolves the original open question)

**Chosen:** Embeddings are generated by **Rosetta** (self-hosted, `https://dev-rosetta.example.com`), OpenAI-compatible `/api/v1/embeddings`. Default model `qwen3-emb-0.6b` → **1024-dim** (confirmed live). SurrealDB vector index dimension is fixed to 1024.

**Why:** Self-hosted/controlled, OpenAI-compatible (no SDK lock-in), **no token/login dance** (unlike the Anthropic `MEMORY_TOKEN_URL` path — see Decision 5, which now applies only to the extraction LLM, not embeddings). Same endpoint for write and query guarantees vector comparability.

**Future routing:** Rosetta also serves `sapbert` (biomedical) and performs entity resolution over HGNC/EFO/dbSNP/GO/ChEMBL/etc. with a hybrid `/api/search`. Given the Example reference use case, later work may (a) route biomedical-entity content to `sapbert`, and (b) normalize captured biomedical entities to canonical ontology IDs before storage. **Not v1 scope.**

**Trade-off:** Rosetta serves the 0.6B (1024d) model, smaller than the 8B (4096d) the original mused. Controlled + adequate beats larger + external; revisit only if recall quality disappoints.

### Decision A5 — Single ingestion cadence; per-turn chunk embedding dropped for v1 (supersedes Decision 8 dual cadence)

**Chosen:** One curated ingestion cadence (Decision A3). The original per-turn `memory.chunks` → embedder → vector-store path is **dropped for v1**.

**Why:** Within its own session a worker already has the context, so vector recall of its own recent turns is pointless. The only unique value of per-turn embedding is **cross-worker real-time** sharing — narrow and unproven — while it reintroduces exactly the conversational noise Engram-curation removes, plus a second pipeline to maintain. Durable knowledge (lessons, recipes, past runs, wiki) — the high-value 90% — is served by the curated path.

**Revisit path:** if cross-worker real-time recall proves necessary, add it back **gated** (embed only `/lesson`-marked or high-signal turns via a cheap filter), never "embed everything".

### Decision A6 — Query via a typed intent API (refines Decision 9)

**Chosen:** The memory service exposes a small, stable, **intent-typed** query API over NATS request-reply — not raw SurrealQL:
- **`recall`** (semantic / vector neighbors),
- **`lookup`** (lexical / full-text),
- **`trace`** (KG / graph-chain traversal).

Fail-open, ~3s timeout (unchanged from Decision 9). The service translates intent → SurrealDB (FTS index / HNSW / `RELATE` traversal). **Why intent-typed:** preserves the backend-swap abstraction — if SurrealDB is later swapped or Spectron lands, agent queries do not break.

**Human access:** because SurrealDB can expose its own RPC/HTTP API, humans get a **direct read-only, scoped** query path for exploration. Agents always go through the memory service for stability.

### Decision A7 — Layered ontology: pinard-core + per-repo domain (supersedes/reframes the original single ontology)

**Chosen:** Two ontology layers for v1:
1. **`pinard-core`** — repo-agnostic agent-operational concepts, mirroring babysitter primitives: `Task`/`Step`, `Verdict`/`Decision`, `Gate` (breakpoint), `Action`, `Diagnosis`, `LogPattern`, `EnvironmentCondition`, `Artifact`; plus the base edges (`DependsOn`, `Produces`, `Consumes`, `IndicatesProblem`, `ResolvedBy`, `RequiresCondition`, `TriggersDecision`). Versioned centrally in pinard.
2. **Per-repo domain ontology** — subclasses core, lives in the repo alongside `process.js`. E.g. `example`: `SlurmJob (is-a TaskExecution)`, `ShardThresholdDecision (is-a Decision)`, `GWASStudy (is-a Artifact)`, `ProvenanceRecord`.

**Why:** The original 8/7 ontology baked Example/HPC specifics (`SlurmJob`, shard `ThresholdDecision`) into what should be generic. Layering keeps core small and stable while domains evolve locally.

**Granularity:** per-repo by default; per-process override only where a process genuinely diverges; per-agent is too granular. A `data-pipeline` mid-layer (between core and domain) is **deferred — but intended soon**.

**Feasibility:** Pydantic inheritance (domain subclasses core); Graphiti `add_episode` accepts per-call `entity_types`/`edge_types` (per-`group_id` composition in the eval sidecar); SurrealDB edge types are `RELATE` relation names, scoped by namespace/DB. A registry composes `core + active domain` per `group_id`.

**Lifecycle & promotion:** `prescribed → learned → promoted`. Learned types emerge in teaching mode; promotion **domain → core** is **human-gated** (git PR), plus a `suppressed_types` list to retire types. Promotion reviews may be driven from Obsidian (checkboxes/forms) that translate to a PR (Decision A8).

**Reproducibility:** every portable memory subset **version-stamps both** the `pinard-core` and the domain ontology versions it was extracted under. Ontology versioning + a migration policy are **new, load-bearing scope** for the ExoHub goal.

### Decision A8 — Wiki: Obsidian markdown ⇄ SurrealDB (refines Decisions 10, 11)

**Chosen:** Markdown files in Obsidian vaults remain the human-facing, git-tracked, durable artifact (preserving Decision 10's "files still readable if Obsidian disappears"). **SurrealDB is the index + curation workspace + serving layer**: pages as documents (body + frontmatter), page embeddings as vectors, backlinks/entity refs as `RELATE` edges — so the wiki is searchable in the same recall path and its graph is natively queryable. No separate Qdrant.

**Bidirectional:** the curator (Sonnet) operates on SurrealDB and emits/updates markdown; a file-watcher/git sync re-ingests human edits back into SurrealDB. Confidence gating (≥0.7 auto-serve, else `needs_review`) governs retrieval visibility (Decision 11 unchanged).

**Human-gated promotion via Obsidian:** promotion decisions (ontology domain→core, cross-scope rule elevation) surface as checkboxes/forms in Obsidian (Dataview/Buttons or a small frontmatter-form→PR bridge), translating an approval into a git PR — low-friction review where humans already explore the wiki.

### Decision A9 — Scope hierarchy & unified promotion (reverses the original cross-group-federation non-goal)

**Chosen:** Knowledge is stored at the finest grain (project = vigne) and **rolled up** by the memory service: **vigne → vignoble → cross-vignoble (global)**. The service knows vignoble membership from `vignes.yaml`. A fact recurring across N projects/vignobles is a **promotion candidate** to a broader scope (e.g. "use `fix:`/`feat:` prefix" seen in multiple vignobles → promote toward the base prompt).

**Explicit reversal:** the original design listed "cross-group knowledge federation" as a **Non-Goal** and scoped everything per `group_id` to prevent bleeding. This amendment makes **controlled upward promotion** an intended capability.

**Unifying insight:** scope roll-up, cross-scope **rule** promotion, and **ontology** promotion (domain→core) are the *same* pattern — recurrence detection + human-gated review. Engram's relation judgment (`related / scoped / supersedes / conflicts_with`) is the candidate engine for recurrence/contradiction detection rather than a custom build.

**Safety:** promotion to anything that mutates global agent behavior (e.g. the base prompt) is **human-gated** — a wrong global rule misbehaves every agent.

## Resulting stack

| Concern | Original | Amended |
|---|---|---|
| Store of record | FalkorDB + Qdrant + git-YAML | **SurrealDB** (doc + graph + vector) |
| KG + NER | Graphiti (primary) | **Graphiti eval sidecar** (jsonl-fed), Phase-2 decision; Spectron slot |
| Capture / curation | all transcripts → Graphiti | **Engram** (`/lesson`, `/teaching`, passive) |
| Embeddings | Qwen3-8B (endpoint undecided) | **Rosetta `qwen3-emb-0.6b`, 1024d** |
| Ingestion cadence | dual (chunks + episodes) | **single curated path** |
| Query | Qdrant+Graphiti blend | **typed intent API** (lexical/semantic/KG) |
| Ontology | single 8/7 (Example-flavored) | **layered** (pinard-core + per-repo domain), version-stamped subsets |
| Wiki | Obsidian + Qdrant | **Obsidian ⇄ SurrealDB**, checkbox/form→PR promotion |
| Portability | git-YAML fallback | **portable embedded SurrealDB subset** (ExoHub-versioned) |

## Tasks impact

- **tasks.md §1 (Infrastructure)** — replace FalkorDB-primary with SurrealDB (server + embedded); keep an eval-only FalkorDB+Graphiti sidecar; Rosetta embedding client.
- **tasks.md §2 (Ontology)** — reframe to `pinard-core` base + layering/registry/composition + version-stamping; move Example terms out.
- **tasks.md §3 (Ingester)** — becomes the **Engram→SurrealDB curated ingester** (pull from RDS/API, embed via Rosetta, map to ontology, write to SurrealDB, run promotion roll-up). Extraction LLM token dance (3.4/3.5) applies only to the Graphiti eval sidecar and teaching extraction, not embeddings.
- **tasks.md §7 (Recipe export)** — repurpose toward `SurrealDB → jsonl → Graphiti` eval export **and** portable-subset export.
- **New task group** — portable memory subset (subset central SurrealDB by scope → embedded file → local load at agent boot; version-stamp ontology).
- **New task group** — scope roll-up + promotion engine (Engram relation judgment; Obsidian form→PR bridge).
- **Per-capability `specs/*/spec.md`** — scenario deltas referencing FalkorDB/Qdrant/dual-cadence to be reconciled per-capability during implementation (tracked, not silently rewritten here).

## Spikes (de-risk before broad implementation)

1. **Engram → SurrealDB curated ingestion**: read surface (Engram API vs RDS schema), embed via Rosetta, store + recall quality on 1024-d vectors, HNSW config.
2. **SurrealDB → jsonl → Graphiti**: export shape, feed Graphiti, assess temporal-KG + NER value/effort → informs the Phase-2 KG decision.

## Open questions

- Engram read surface: supported API vs direct RDS schema (spike 1 answers this).
- SurrealDB subset/export-to-embedded-file: tooling to build (`SELECT` by scope/NS-DB → fresh embedded instance).
- Ontology migration policy when core/domain versions change under already-shipped subsets.
- Promotion thresholds (how many scopes before a rule/type is a candidate).
- Data-pipeline mid-layer timing (deferred but intended soon).
