> **⚠️ Amended by [`amendment-01-surrealdb.md`](./amendment-01-surrealdb.md).** The architecture pivoted to **SurrealDB** (store of record) + **Engram** (curated write path) + **Rosetta** (embeddings), with **Graphiti/FalkorDB** demoted to a time-boxed evaluation sidecar, **Qdrant dropped**, a **layered ontology** (pinard-core + per-repo domain), and **portable, versioned memory subsets**. Read the amendment for the authoritative stack; the text below is retained for audit and remains valid except where the amendment overrides it.

## Why

Pinard workers are stateless across sessions. When a long-running data pipeline worker crashes or a new session starts, accumulated operational knowledge (failure patterns, recovery recipes, threshold decisions) is lost. Humans must re-teach the same lessons on every run. This blocks Pinard's expansion into multi-day/week data pipeline orchestration where agents must learn and improve over time.

## What Changes

- Add a **temporal knowledge graph** (Graphiti, Neo4j-backed) that stores operational facts extracted from agent conversations — with validity windows so stale knowledge auto-expires
- Add an **episode ingester** service (NATS consumer) that captures conversation transcripts and calls Graphiti's LLM-based extraction with a Pydantic-defined ontology
- Add a **`/teaching` toggle** on the conductor that signals aggressive extraction mode during human teaching sessions
- Add **worker boot injection** — workers query accumulated recipes for their pipeline's `group_id` at startup via NATS request-reply (with git-based fallback)
- Add a **recipe exporter** cron job that serializes current facts to `recipes/{group_id}/current.yaml` and commits to git
- Define a **prescribed ontology** (8 entity types, 7 edge types) tailored to data pipeline operations, with a lifecycle for learned types to emerge from teaching and be promoted to prescribed
- Scope all knowledge per pipeline via `group_id` to prevent cross-pipeline bleeding

## Capabilities

### New Capabilities

- `memory-layer`: Core memory architecture — episode ingestion, Graphiti integration, ontology model, temporal validity, cross-pipeline scoping, cost model, observability
- `teaching-mode`: The `/teaching` toggle on the conductor — activation, deactivation, retroactive capture (`--from`, `--all`), extraction aggressiveness signal
- `recipe-export`: Git export mechanism — cron-driven YAML serialization, commit strategy, multi-group support
- `boot-injection`: Worker boot context injection — group_id assignment, NATS request-reply query, git fallback, injection format
- `mid-session-retrieval`: Per-turn knowledge recall via NATS request-reply to a memory service that blends Qdrant (semantic) and Graphiti (structured) results, with server-side summarization, relevance gating, and session dedup
- `wiki-curation`: Self-evolving Obsidian wiki — raw input channels (MRs, teachings, incidents, specs, fact exports), LLM-powered curation (Sonnet), confidence gating with human review, continuous ingest to Qdrant, staleness management

### Modified Capabilities

- `worker`: Worker extension gains `message_end` hook for episode publishing (session-end to Graphiti), per-turn chunk publishing (to Qdrant via memory.chunks), `session_start` hook for recipe query/injection, and per-turn recall query to memory service
- `conductor`: Conductor gains `/teaching` command registration and teaching-mode state management

## Impact

- **New service**: Episode ingester (Python or Go NATS consumer, standalone deployment)
- **New service**: Memory service (handles recall queries, chunk embedding, blends Qdrant + Graphiti, summarizes with Haiku)
- **New service**: Wiki curator (NATS consumer + periodic scanner, processes raw docs into structured wiki pages using Sonnet)
- **New infrastructure**: FalkorDB instance for Graphiti, Qdrant instance for vector search, embedding model endpoint
- **New dependencies**: `graphiti-core` Python package, Pydantic ontology models, Qdrant client, embedding model API
- **Modified extensions**: `pi-extension/worker/index.ts` (episode publish hook, per-turn chunk publish, per-turn recall query, boot recipe query), `pi-extension/pinard/index.ts` (`/teaching` command)
- **New NATS subjects**: `memory.chunks` (per-turn to Qdrant), `memory.recall` (request-reply), `memory.wiki.raw` (wiki inputs)
- **New NATS stream**: `pinard-memory` covering `pinard.{vignoble}.memory.>` (episodes, chunks, wiki)
- **New file artifacts**: Per-vignoble `.wiki/` Obsidian vaults, global wiki at `~/.pinard/wiki/`, `recipes/` directory with per-group YAML files
- **LLM cost**: Haiku per recall query (summarization, ~400 tokens), Haiku per episode extraction (Graphiti), Sonnet per wiki curation (classification + page generation, infrequent)
- **Deployment on HPC**: Qdrant runs as static binary (no Docker), Redis as system package or Singularity, all Python services in virtualenvs
