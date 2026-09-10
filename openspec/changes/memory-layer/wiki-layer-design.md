# Wiki Layer — Step-0 Design

Status: **Draft** · Change: `memory-layer` · Builds on: `amendment-01-surrealdb.md`
(Decision A8), `design.md` (Decisions 10–11).

This document fixes the contracts for the wiki layer **before** implementation so
the pieces (format, ontology, storage, sync, curator, serving) agree. It does not
schedule work — see the linked issues.

---

## 1. Purpose & scope

The wiki layer turns accumulated, ontology-typed memory into a **curated,
human-readable, git-tracked knowledge base** that is (a) browsable/editable by
humans, (b) searchable in the same recall path as raw memory, and (c) organized
by the **same ontology** that types the memory graph — per `group_id`
(vigne/pipeline), plus a global wiki.

The wiki is a **synthesis/serialization layer** over the memory in SurrealDB. It
is *not* a new taxonomy and *not* a new store of record.

---

## 2. Principles

1. **Markdown-in-git is the source of truth.** SurrealDB is a *rebuildable,
   derived* index + serving layer (already proven: it re-ingests from the RDS
   source of truth). If Obsidian/SurrealDB disappear, the markdown still reads.
2. **Format = Google OKF v0.1** (Open Knowledge Format) — a directory of
   markdown files + YAML frontmatter. Vendor-neutral, diffable, portable, no SDK.
3. **Organization = the ontology.** Concept `type` ← ontology entity role; links
   ← ontology edge types; taxonomy per `group_id` from `registry.compose()`.
   The wiki does not invent structure — it fills a schema the ontology already
   defines.
4. **Open-world, never drop.** Knowledge the ontology can't yet express is
   **staged**, not discarded, and mined to evolve the ontology (issues #104/#105).
5. **Human-gated structural change.** Content flows freely; *structural* changes
   (new type, new top-level domain, cross-scope elevation) go through a PR
   (`pr_bridge.py`).
6. **Never expose SurrealDB to laptops.** Sync is via git (issue #103).

---

## 3. Format: OKF v0.1 adoption

An OKF **bundle** = a directory tree of markdown **concept** files. Each concept
is one file; its **path is its ID** (`decisions/surrealdb-pivot.md` →
`decisions/surrealdb-pivot`). Reserved: `index.md` (dir listing / progressive
disclosure), `log.md` (change history). Standard markdown **links between
concepts form the relationship graph**.

### 3.1 Frontmatter contract

```yaml
---
type: Decision              # REQUIRED — an ontology role (see §4)
title: SurrealDB pivot      # recommended
description: One-sentence summary for index/search snippets.
tags: [storage, memory]
timestamp: 2026-07-22T01:00:00Z
# --- pinard extensions (OKF allows producer keys; consumers preserve them) ---
group_id: example-build      # tenant scope (which composed ontology applies)
ontology_version: "core-1.0.0+example-1.0.0"
role: decision              # ontology entity role (lowercase); == type, normalized
confidence: 0.82            # [0,1]
status: auto_serve          # needs_review | auto_serve  (Decision A8/11: ≥0.7 → auto_serve)
source: curator             # curator | human
content_hash: sha256:…      # idempotency / loop-avoidance
relations:                  # typed links (§4.2), materialized as RELATE edges
  - edge: ResolvedBy
    to: actions/rewrite-surreal-client
---
```

Only `type` is required by OKF; everything else is recommended or a pinard
extension. Consumers MUST tolerate unknown `type`s and preserve unknown keys.

### 3.2 Body

Standard markdown, structural over prose. Conventional sections (OKF §4.2):
`# Schema`, `# Examples`, `# Citations`. Citations are links to sources
(observations, MRs, incidents) backing claims.

### 3.3 Free wins from OKF

- The single-file **HTML graph visualizer** browses any bundle (no backend).
- Ingestible by Google Knowledge Catalog and any OKF consumer.
- Interop with other OKF producers (e.g. OpenWiki — see §8).

---

## 4. Ontology-driven organization

The wiki organizes strictly within `registry.compose(group_id)` (core + domain −
suppressed), which is **reflected in SurrealDB** by issue #104
(`ontology_entity_type`, `ontology_edge_type`, `ontology_version`).

### 4.1 `type` ← entity role

`wiki_doc.type` (new field) must be a **role in `ontology_entity_type`** for that
`group_id`: core (`decision`, `diagnosis`, `runbook`/`playbook`, `pattern`,
`artifact`, …) or domain (`gwas_study`, `slurm_job`, …). A `type` not in the
composed set is **not rejected** — the page is staged `needs_review` and feeds the
refinement loop (#105), never dropped.

### 4.2 Links ← edge types

Typed cross-links use ontology **edge types** (`ResolvedBy`, `Produces`,
`DependsOn`, …), validated against `ontology_edge_type.valid_pairs`
`(src_role → tgt_role)`. Each link materializes as a `wiki_references`
(wiki↔wiki) or `wiki_mentions` (wiki↔entity) RELATE edge carrying the edge type.
Illegal pairs are staged, not dropped.

### 4.3 Hierarchy & the "constitution"

- **Folders** group by domain area (per `group_id`) + entity category.
- The **constitution** (per-vignoble + global `INSTRUCTIONS.md`) is *generated*
  from `compose(group_id)` (allowed types + edges) **plus** a small human-authored
  brief for scope/priorities. This is the steering lever; the curator organizes
  within it.
- **New type / new domain / cross-scope elevation** → a `register_domain(...)`
  extension PR (`pr_bridge.py`), human-gated. This *is* Decision A8's
  "ontology domain→core promotion."

---

## 5. Open-world / staging (never discard)

A strict ontology would silently lose novel knowledge and never learn its gaps.
Instead (issues #104/#105):

- Extraction/curation **classifies**: matches the composed ontology → typed
  store; doesn't → **staging** (`entity_staging`/`edge_staging`) with proposed
  type + rationale + provenance + **embedding** + `occurrence_count`.
- Staged items remain **embedded + recall-searchable** as `needs_review` — captured
  and findable, just not first-class in the typed graph.
- The **ontology gardener** (#105) periodically clusters staging, and for recurrent
  clusters proposes a mapping or a `DomainOntology` extension via PR. On merge:
  `compose()` grows → schema regen (#104) → matching staged items **migrate** into
  the typed graph → `ontology_version` bumps.
- **No hard DB `ASSERT`-reject on `role`.** Enforcement is *route-to-staging*, not
  error. Staging growth + top clusters = an ontology coverage-gap signal.

---

## 6. Storage mapping (SurrealDB)

Per `group_id` database (schema generated by #104):

| Concern | Table / mechanism |
|---|---|
| Wiki page | `wiki_doc` (+ **new** `type` field; `path`=concept ID; `frontmatter`, `body`, `confidence`, `status`, `embedding`) |
| Wiki↔wiki links | `wiki_references` RELATION (+ edge `type`) |
| Wiki↔entity links | `wiki_mentions` RELATION (+ edge `type`) |
| Typed memory graph | `entity` + composed edge RELATION tables |
| Ontology meta-model | `ontology_entity_type`, `ontology_edge_type`, `ontology_version` (#104) |
| Unclassified knowledge | `entity_staging`, `edge_staging` (#104/#105) |

Recall unifies `wiki_doc` + `entity` via the shared 1024-d HNSW + BM25 FTS
indexes, filtered by `status` (§7).

---

## 7. Sync (git backbone — issue #103)

**Decision (parked-but-decided): git now, live-sync later, never expose SurrealDB.**

- **Wiki repo(s):** an OKF bundle in git — per-`group_id` `.wiki` + a global wiki.
  Bot deploy token for the cluster; humans use normal git creds.
- **Laptop / another server:** clone into an Obsidian vault; `obsidian-git`
  plugin (pull on open, auto commit+push). Offline-capable. Headless server = cron
  pull/push.
- **Outbound (SurrealDB→md):** curator renders `wiki_doc`→OKF markdown, commits +
  pushes.
- **Inbound (md→SurrealDB):** git-sync pulls, diffs changed `.md`, parses OKF
  frontmatter+body, **re-embeds via Rosetta**, upserts `wiki_doc` + links, sets
  `status`. Idempotent via `content_hash`.
- **Loop-avoidance:** `source`/`content_hash`/`last_ingested_seq` in frontmatter;
  human edits authoritative (curator augments only on new raw inputs); curator
  drafts stay `needs_review`; git handles textual merges.
- **Promotion:** checkbox→MR via `pr_bridge.py`.
- **Live-sync (obsidian-livesync-style) is parked** (#103) — SurrealDB has the
  primitives (`LIVE SELECT`/`CHANGEFEED`) if real-time is ever needed, behind a
  gateway, never direct.

---

## 8. Curator: thin & ontology-driven (build, don't adopt OpenWiki)

- **Producer:** a thin curator (NATS consumer on `memory.wiki.raw` + periodic
  scanner) that reads the **typed SurrealDB graph** for a `group_id`, clusters
  related entities/edges, and synthesizes OKF concept pages **within
  `compose(group_id)`** using `build_llm_client()` (model-agnostic #73;
  Gemini-on-Vertex configured). Renders markdown → commit → MR.
- **Incremental:** only (re)synthesize concepts whose source observations changed
  (via `seq`/`content_hash` cursors); **dedup by embedding** before creating a
  near-duplicate; maintain `index.md`/`log.md`; a periodic "gardening" pass merges
  near-dups, fixes orphan links, prunes stale.
- **Why not OpenWiki (`langchain-ai/openwiki`) as the curator:** it synthesizes
  from *its* sources (git/Notion/Gmail/…), has **no SurrealDB source**, is a
  single-user CLI (vs our multi-tenant per-`group_id`), and has **no formal
  ontology** (no type validation, typed edges, or per-tenant composed taxonomy) —
  our core differentiator. We **adopt OKF (the format)** regardless, and may reuse
  its patterns (`INSTRUCTIONS.md` brief, templates, `index.md`/`log.md`, auto-MR CI).
- **Separate win:** OpenWiki **code-mode** (repo docs + `AGENTS.md`/`CLAUDE.md`,
  GitLab-CI auto-MR) is independently useful for the pinard repo's own agent docs —
  low effort, unrelated to the memory-wiki.

---

## 9. Serving / recall

Git bundle → parse OKF → Rosetta embed → `wiki_doc` + typed links → **confidence-
gated recall**: `status = auto_serve` (`confidence ≥ 0.7`) served by default;
`needs_review` (incl. staged/unclassified) hidden unless explicitly requested.
Wiki pages and raw entities are returned through one recall path (vector + FTS +
graph), scoped by `group_id` (global wiki always included).

---

## 10. Build order & open questions

**Order (each independently testable, bottom-up):**
1. **#104** — reflect composed ontology in SurrealDB (schema-gen + meta-model +
   open-world staging). *Blocks the wiki.*
2. Step-0 format/schema deltas: add `wiki_doc.type`; relax `wiki_doc_title` UNIQUE
   → **path-unique**; index/log generation contract.
3. Inbound git-sync + re-ingest (testable against hand-written OKF pages, no LLM).
4. Curator (raw/graph → OKF → MR), incremental + dedup.
5. `memory.wiki.raw` raw-input feeds (start with merged MRs).
6. Confidence-gated serving in recall.
7. **#105** — ontology gardener (staging → extension PRs) — can land after 1–6.
8. Deploy: `charts/pinard` wiki-curator component + git bot token (Vault) +
   "Stage 10: wiki round-trip" in the test plan; laptop-setup doc.

**Open questions:**
- Wiki repo topology: one repo with per-`group_id` subdirs vs. one repo per vigne
  + a global repo? (leaning: per-vigne `.wiki` + global, mirroring vignoble layout.)
- Global wiki curation authority + how cross-vignoble elevation is gated.
- `type` vs `role` normalization (display-cased `type` vs lowercase `role`) — keep
  both or derive one?
- Rate limits / cost for the curator LLM at scale (calibrate from raw volume).
