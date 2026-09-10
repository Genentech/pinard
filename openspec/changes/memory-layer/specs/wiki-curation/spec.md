## ADDED Requirements

### Requirement: Wiki Architecture

Each vignoble SHALL have an Obsidian-compatible wiki vault that serves as a curated, human-reviewable knowledge base. A global wiki at the pinard level provides cross-vignoble knowledge.

```
Inputs (raw/)                    Curation Pipeline              Qdrant
─────────────────               ──────────────────             ──────
Merged MR descriptions    ──►                                    ▲
Teaching transcripts      ──►   Curator Service                  │
Incident notes (manual)   ──►   (LLM: Sonnet)        ──►  Wiki Pages ──► Continuous Ingest
Graphiti fact exports     ──►     │                              │
Spec/architecture commits ──►     │                              │
Worker session summaries  ──►     ▼                              │
                              SCHEMA.md rules                     │
                              index.md catalog                    │
                              log.md audit                        ▼
                                                            Qdrant (knowledge_base)
```

#### Scenario: Per-vignoble wiki vault
- **WHEN** a vignoble is initialized
- **THEN** a wiki vault is created at `{vignoble}/.wiki/`
- **AND** it contains a `SCHEMA.md`, empty `raw/`, `concepts/`, `runbooks/`, `decisions/`, `entities/`, `patterns/` directories
- **AND** it is a valid Obsidian vault (contains `.obsidian/` directory with minimal config)

#### Scenario: Global wiki vault
- **WHEN** the pinard system is initialized
- **THEN** a global wiki vault exists at a configurable path (default: `~/.pinard/wiki/`)
- **AND** it covers cross-vignoble knowledge: pinard architecture, common patterns, tool usage, operational procedures
- **AND** it is indexed into Qdrant with `group_id: "_global"` (always included in searches regardless of worker's group_id)

---

### Requirement: Obsidian Compatibility

Wiki pages SHALL be standard markdown files with YAML frontmatter, using Obsidian wikilink syntax for inter-page references.

#### Scenario: Page format

```markdown
---
type: runbook
title: "Resolving OOM in TileDB optimize step"
group_id: example-build
tags: [slurm, oom, step5, tiledb]
sources:
  - raw/incidents/2026-05-28-oom.md
  - graphiti:fact-42
confidence: 0.85
needs_review: false
last_verified: 2026-05-28
created_by: curator
---

## Problem

TileDB optimize step OOMs when processing shards with more than 30k studies.

## Resolution

Increase `--mem-budget` to 64G for the SLURM job. See [[shard-threshold-tuning]] for
how to determine the right budget based on shard size.

## Related

- [[slurm-job-chaining]] — how optimize chains after the index step
- [[example-step5]] — full step5 documentation
```

#### Scenario: Wikilinks connect pages
- **WHEN** the curator creates or updates a page
- **THEN** it uses `[[page-name]]` syntax to link to related pages
- **AND** Obsidian renders these as navigable links in graph view
- **AND** broken wikilinks (pointing to non-existent pages) are acceptable — they signal pages that should be created

#### Scenario: Frontmatter required fields
- **WHEN** a wiki page is created
- **THEN** it MUST contain frontmatter with: `type`, `title`, `tags`, `sources`, `confidence`, `needs_review`, `created_by`
- **AND** `type` is one of: `concept`, `runbook`, `decision`, `entity`, `pattern`
- **AND** `tags` uses the closed taxonomy defined in `SCHEMA.md`
- **AND** `confidence` is a float 0.0-1.0 reflecting source reliability
- **AND** `created_by` is `"curator"` (automated) or `"human"` (manually written)

---

### Requirement: Raw Input Channels

The wiki's `raw/` directory is the intake point for all knowledge sources. Multiple processes feed it.

#### Scenario: Merged MR descriptions
- **WHEN** the daemon's MR watcher detects a merge
- **THEN** it publishes the MR title, description, and project to `pinard.{vignoble}.memory.wiki.raw`
- **AND** the wiki service writes it to `raw/mrs/{date}-{mr-iid}.md` with frontmatter: `{source: "mr", project, mr_url, merged_at}`

#### Scenario: Teaching session transcripts
- **WHEN** the episode ingester processes an episode with `mode: "teaching"`
- **THEN** it also publishes the episode to `pinard.{vignoble}.memory.wiki.raw`
- **AND** the wiki service writes it to `raw/teachings/{date}-{session_id}.md`

#### Scenario: Graphiti fact exports
- **WHEN** the recipe exporter cron runs (already spec'd)
- **THEN** it also writes a summary of new/changed facts to `raw/facts/{date}-{group_id}.md`
- **AND** the curator can synthesize these into runbook updates

#### Scenario: Spec and architecture changes
- **WHEN** a git commit modifies files under `openspec/` or `VIGNE.md` in a vignoble's project
- **THEN** the daemon detects the change (via existing config hot-reload watcher pattern)
- **AND** publishes the diff summary to `pinard.{vignoble}.memory.wiki.raw`
- **AND** the wiki service writes it to `raw/specs/{date}-{filename}.md`

#### Scenario: Manual input (human-written)
- **WHEN** a human places a file directly in `raw/incidents/` or any `raw/` subdirectory
- **THEN** the curator picks it up on next run
- **AND** the file is treated as high-confidence source material (`confidence >= 0.9`)

#### Scenario: Worker session summaries (batch)
- **WHEN** the daily summary cron runs
- **THEN** it queries Graphiti for sessions in the last 24h grouped by `group_id`
- **AND** writes a summary to `raw/sessions/{date}-summary.md`
- **AND** this feeds the curator for potential runbook/pattern/concept extraction

---

### Requirement: NATS Subjects (Wiki)

| Pattern | Direction | Used by |
|---------|-----------|---------|
| `pinard.{vignoble}.memory.wiki.raw` | publish (JetStream) | Daemon/Ingester → Wiki Service |

#### Scenario: Stream coverage
- **WHEN** the `pinard-memory` stream is provisioned
- **THEN** it SHALL also cover `pinard.{vignoble}.memory.wiki.>`
- **AND** the durable consumer for the wiki service SHALL be `pinard-memory-wiki-{vignoble}`

---

### Requirement: Curation Pipeline

The curator service SHALL process raw input into structured wiki pages using an LLM (Sonnet-tier).

#### Scenario: Curation trigger
- **WHEN** new files appear in `raw/` (detected via NATS subscription or periodic scan)
- **THEN** the curator processes them in FIFO order

#### Scenario: Curation process
- **WHEN** the curator processes a raw file
- **THEN** it reads `SCHEMA.md` for taxonomy rules and page quality standards
- **AND** it reads `index.md` for existing page inventory
- **AND** the LLM analyzes the raw content and decides: create new page, update existing page, or skip (trivial/duplicate)
- **AND** if creating: generates a page in the appropriate directory (`concepts/`, `runbooks/`, etc.) with full frontmatter and wikilinks
- **AND** if updating: edits the existing page, preserving human edits and appending new information
- **AND** logs the action in `log.md`

#### Scenario: Page type classification
- **WHEN** the LLM analyzes raw content
- **THEN** it classifies the output as one of:
  - `runbook` — operational procedure: problem + resolution + verification steps
  - `concept` — abstract idea or pattern: what it is, when to use it, tradeoffs
  - `decision` — architectural or design choice: context, options considered, chosen approach, rationale
  - `entity` — concrete thing: tool, service, project, pipeline step
  - `pattern` — recurring solution shape: when it applies, structure, examples
  - `skip` — trivial, duplicate, or not worth a page

#### Scenario: Existing page detection
- **WHEN** the curator is about to create a new page
- **THEN** it checks if a page with high semantic overlap already exists (title similarity + tag overlap)
- **AND** if a match is found, it updates that page instead of creating a duplicate
- **AND** the update appends new information under a `## Updates` section or integrates inline

#### Scenario: SCHEMA.md as constitution
- **WHEN** the curator creates any page
- **THEN** it adheres to rules defined in `SCHEMA.md`:
  - Closed tag taxonomy (no ad-hoc tags)
  - Required sections per page type
  - Minimum quality bar (at least one wikilink, concrete details, not vague)
  - Maximum page length (prevent bloat)

---

### Requirement: Confidence Gating and Human Review

Pages SHALL have confidence scores that determine their injection eligibility.

#### Scenario: Confidence assignment
- **WHEN** the curator creates a page
- **THEN** confidence is determined by source reliability:
  - Human-written raw input → `confidence: 0.9`
  - Teaching session transcripts → `confidence: 0.8`
  - Graphiti facts with multiple corroborating episodes → `confidence: 0.75`
  - Single autonomous worker session → `confidence: 0.5`
  - MR description alone (no verification) → `confidence: 0.6`

#### Scenario: Human review gating
- **WHEN** a page is created with `confidence < 0.7`
- **THEN** it is tagged `needs_review: true`
- **AND** it is stored in the wiki vault (visible in Obsidian)
- **AND** it is NOT indexed into Qdrant until reviewed

#### Scenario: Human approves a page
- **WHEN** a human edits a page and sets `needs_review: false` (or removes the field)
- **THEN** the next continuous ingest run picks it up and indexes it into Qdrant
- **AND** `created_by` is updated to `"human"` if the human made substantive edits

#### Scenario: High-confidence auto-publish
- **WHEN** a page is created with `confidence >= 0.7`
- **THEN** `needs_review: false` is set
- **AND** the page is eligible for Qdrant indexing on the next continuous ingest cycle

---

### Requirement: Continuous Ingest (Wiki → Qdrant)

Wiki pages SHALL be embedded into Qdrant via a periodic ingest pipeline.

#### Scenario: Ingest trigger
- **WHEN** the continuous ingest cron runs (recommended: hourly)
- **THEN** it scans all wiki pages in `{vignoble}/.wiki/` (excluding `raw/`)
- **AND** computes SHA-256 hash for each file
- **AND** compares with state file (`{vignoble}/.wiki/.ingest-state.json`)
- **AND** new or modified pages are queued for embedding

#### Scenario: Ingest filtering
- **WHEN** a page has `needs_review: true`
- **THEN** it is skipped (not embedded into Qdrant)

#### Scenario: Embedding and upsert
- **WHEN** a page is queued for embedding
- **THEN** its content is embedded using the same model as the chunk embedder (consistent vector space)
- **AND** it is upserted into Qdrant with payload: `{source: "wiki", type, title, tags, group_id, confidence, last_verified, vignoble}`
- **AND** the point ID is deterministic from file path (allows update-in-place)

#### Scenario: Global wiki ingest
- **WHEN** the continuous ingest runs
- **THEN** it also ingests the global wiki (`~/.pinard/wiki/`)
- **AND** global pages are stored with `group_id: "_global"`
- **AND** the memory service includes `_global` results in all recall queries regardless of the worker's `group_id`

---

### Requirement: Staleness and Decay

Wiki pages SHALL not accumulate indefinitely without validation.

#### Scenario: Staleness detection
- **WHEN** the weekly maintenance cron runs
- **THEN** it scans all wiki pages for `last_verified` older than 90 days
- **AND** pages with no access from the memory service (not returned in any recall response) in 90 days are flagged `needs_review: true`
- **AND** their Qdrant points are removed until re-verified

#### Scenario: Contradiction-triggered archival
- **WHEN** the Graphiti episode ingester detects a contradiction that invalidates a fact
- **AND** that fact is sourced from a wiki page (via `sources` field linkage)
- **THEN** the wiki service marks the page as `needs_review: true`
- **AND** adds a note: `## Flagged\nContradicted by episode {session_id} on {date}: {new_fact}`

#### Scenario: Manual archival
- **WHEN** a human moves a page to `_archive/`
- **THEN** the next ingest run removes its Qdrant point
- **AND** the page remains on disk for historical reference

---

### Requirement: Index and Audit Trail

#### Scenario: index.md auto-generation
- **WHEN** the curator creates or updates a page
- **THEN** it regenerates `index.md` with one-line entries for all non-archived pages
- **AND** entries are grouped by type (concepts, runbooks, decisions, entities, patterns)
- **AND** each entry includes: title, tags, confidence badge, last_verified date

#### Scenario: log.md audit
- **WHEN** the curator performs any action (create, update, skip, archive)
- **THEN** it appends to `log.md`: timestamp, action, page path, source raw file, brief rationale
- **AND** `log.md` is never auto-truncated (human responsibility to prune)

---

### Requirement: Curator Deployment

#### Scenario: Curator as NATS consumer
- **WHEN** the curator service starts
- **THEN** it subscribes to `pinard.{vignoble}.memory.wiki.raw` (durable consumer)
- **AND** it processes incoming raw documents
- **AND** it also runs a periodic scan (hourly) for files placed directly in `raw/` without NATS notification

#### Scenario: Curator LLM configuration
- **WHEN** the curator processes a raw document
- **THEN** it uses a mid-tier model configured via `WIKI_CURATOR_MODEL` env var
- **AND** defaults to `claude-sonnet-4-6` if not set
- **AND** the model is used for: classification, page generation, existing page detection, and update integration

#### Scenario: Curator cost model
- **WHEN** the curator processes raw documents
- **THEN** each document costs approximately one Sonnet call (classification + generation in one pass)
- **AND** the hourly scan processes at most 10 documents per run (rate limiting)
- **AND** documents exceeding the rate limit are processed in the next cycle (FIFO)

---

### Requirement: Observability

#### Scenario: Curator status file
- **WHEN** the curator completes a processing cycle
- **THEN** it updates `{vignoble}/logs/wiki-curator-status.json`:

```json
{
  "state": "idle",
  "last_run_at": "2026-05-25T15:00:00Z",
  "pages_total": 47,
  "pages_needing_review": 3,
  "raw_pending": 2,
  "pages_created_today": 1,
  "pages_updated_today": 2,
  "pages_skipped_today": 4
}
```

#### Scenario: Ingest status file
- **WHEN** the continuous ingest cron completes
- **THEN** it updates `{vignoble}/logs/wiki-ingest-status.json`:

```json
{
  "last_run_at": "2026-05-25T15:00:00Z",
  "pages_indexed": 44,
  "pages_skipped_review": 3,
  "pages_embedded_this_run": 2,
  "qdrant_collection": "knowledge_base",
  "total_points": 156
}
```
