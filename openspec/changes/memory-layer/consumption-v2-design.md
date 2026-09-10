# Memory Consumption v2 — tiers, promotion, and boot manifest

Status: design (2026-08) · Supersedes the "boot serves raw hits" behavior shipped in #155.

This note captures a set of cohesive refinements to how accumulated knowledge is
**consolidated across tiers** and **consumed by agents**. It builds on the live
pipeline (image 0.26.0): ingester → SurrealDB entities → curator → `wiki_doc`;
rollup consolidates across the scope hierarchy; recall (#156) + boot injection
(#155) serve it.

## 1. Three layers, three jobs

The system holds **three** distinct kinds of knowledge. Conflating them (as an
earlier draft did — lumping typed knowledge in with raw memory) is the root of the
problems below. They are **complementary peers**, not a hierarchy where wiki is the
only first-class citizen.

| Layer | What it is | Job for the agent | In Engram? |
|-------|-----------|-------------------|-----------|
| **1. Raw memory** | `artifact` entities — ~1:1 verbatim Engram observation mirrors, untyped/unprocessed | none special; noise | **yes** (redundant) |
| **2. Typed knowledge (the graph)** | ontology entities: `decision`, `diagnosis`, `gotcha`, … — granular, structured, machine-queryable bits | *precise, task-shaped facts* — "this exact decision was made", "this gotcha bites here" | structured mirror |
| **3. Wiki (`wiki_doc`)** | synthesized OKF prose, **git-backed, human-editable** | the *blessed overview / orientation*; the **human↔agent contract** | **no** — genuinely new |

**Wiki is the joining layer between human and agent** — populated by curation
*and* by humans, the "official" distilled knowledge. **Typed knowledge (Layer 2) is
a first-class peer**, serving a *different, granular* purpose (the queryable graph).
Only **Layer 1 (raw `artifact`)** is the one to deprioritize.

**Important nuance about *today's* Layer 2.** An entity's `role` is currently
assigned **mechanically at ingest** by `type_map` (it relabels the source
observation's own type). So typing today is crude — *not* the same as curation, and
a freshly-typed `decision` entity is still fairly raw. That is a reason to **improve
Layer 2's quality** (curation, #147), **not** to ignore Layer 2. The distinction
"typing ≠ curation" is about the *mechanism*, not a verdict that typed knowledge is
worthless.

Corollary — **pinard is a curated *lens* over Engram, not a superset.** Layer 1
fully overlaps the agent's own Engram; Layers 2–3 (structured graph + synthesized,
human-blessed wiki, plus cross-tier consolidation) are the delta.

## 2. Tier invariant: only curated knowledge rises

**Problem (live).** `rollup.py` promotes entities to `vignoble-<name>` / `__global__`
by **cross-vigne overlap regardless of type**, so raw `artifact`s leak upward. A
boot query at `__global__` returned a verbatim raw memory (`## Goal — sync the
Ledger`). Higher tiers end up holding raw copies, not synthesis.

**Invariant.**
> No **entity** (Layer 1 *or* Layer 2) is ever copied up. The **form** promotion
> produces at a higher tier is always **wiki** (the joining layer) — but it is
> **fed by** Layer 2 (typed knowledge) + lower-tier wiki.

So Layer 2 is *not* a dead-end: it feeds promotion (as synthesis input) and is
surfaced directly at the agent's own tier (§3). It simply crosses tiers **as wiki**,
not as a raw copy.

**Mechanism — curate-on-promote.** Higher tiers are built by *synthesizing*, not
copying:
- Rollup detects cross-vigne overlap of knowledge worth broadcasting (recurring
  wiki pages / typed-knowledge topics).
- The higher-tier curator synthesizes a **new consolidated `wiki_doc`** at the
  vignoble/global scope (e.g. "this decision/gotcha recurs in projects X and Y →
  the generalized version"). No entity is ever lifted verbatim.

Result: each tier is semantically distinct — vigne = raw + typed graph + local
wiki; vignoble = cross-vigne synthesized wiki; global = cross-vignoble synthesized
wiki. The joining layer (wiki) is what spans scopes.

## 3. Boot injection v2: a manifest, not a dump

**Problems (live, #155).** Boot injected **full content** and **raw hits**: (a)
full `wiki_doc` bodies risk blowing up the context window; (b) same-scope raw
entities duplicate Engram; (c) titles are raw fragments (`## Goal`, mid-sentence).

**Design — progressive disclosure.** Boot emits a compact, scope-grouped **index**;
the agent drills into full content on demand.

- Each entry is **one line**: `type · title · one-line summary · ref`.
- **No bodies.** Volume bounded by *entries per scope*, not snippet length.
- **Full content on demand:** the agent calls `recall(fetch=ref)` (see §4) to
  expand only what it judges relevant — index-then-retrieve.
- **No LLM in the hot path:** the one-line summary is the wiki title / first
  sentence (already curated). (A Gemini digest remains a future, flagged,
  fail-open option.)

**Boot surfaces Layers 2 *and* 3 — not wiki-only.** They are complementary: wiki
gives the blessed overview + human guidance; typed knowledge gives granular,
task-relevant facts. Only Layer 1 (raw `artifact`) is dropped (Engram covers it).

**What each tier contributes to the manifest**
- **global / vignoble:** curated **wiki** index entries (these tiers are wiki by
  construction, per §2).
- **vigne (agent's own):** **wiki** entries **+ top typed-knowledge** entries
  (`decision`/`gotcha`/`diagnosis`/…).

Typed knowledge is **not promoted** (§2) but is **first-class at the vigne tier**
and never hidden: it appears as compact index lines so the agent gets precise facts
and can drill in. Access is guaranteed by the `ref` + fetch, not by inlining.

## 4. Drill-down: `recall` fetch-by-ref

Boot entries carry a resolvable `ref` (wiki `path`/OKF id for wiki; entity id for
entities). The agent expands a `ref` on demand.

**Important:** cross-tier wiki is **not** in the agent's local repo (it lives in
another vigne's `wiki/` or `pinard-wiki`), so drill-down must go **through the
memory service** — not assume a local file. Add an **exact `fetch(path|id)` mode**
to the recall tool (#156) alongside its query mode. The server resolves the ref
against SurrealDB (any scope) and returns the full body.

## 5. Layer-2 quality (#147 reframed)

Re-typing `artifact` → `decision`/`diagnosis`/`gotcha` etc. is **not** a promotion
gate (no entity rises, §2). It has **two** distinct payoffs:
1. **Sharper Layer 2** — better-typed knowledge is directly more useful in boot
   (§3) and in recall, because the agent gets crisp, correctly-labeled facts.
2. **Better curator input** — role-aware clustering + role-based confidence bonuses
   (#146) produce better wiki pages and push more across the `auto_serve`
   threshold.

So #147 improves **Layer 2 itself** *and* feeds Layer 3 — decoupled from promotion.

**Title quality becomes load-bearing.** In v2 the title *is* the index line, so
raw-fragment titles (`## Goal`) are a real defect, not cosmetic. Curated `wiki`
titles must be clean sentences; entity index lines should use a derived clean name.

## 6. Work items

- **rollup: promote curated knowledge only** — curate-on-promote; wiki-only rises;
  never lift raw entities (§2).
- **boot injection v2** — compact manifest (`type · title · summary · ref`), no
  bodies; surfaces **wiki + typed knowledge** (wiki-by-construction above vigne;
  wiki + typed-knowledge index at vigne); only raw `artifact` dropped (§3).
- **recall fetch-by-ref** — exact `fetch(path|id)` mode, server-resolved across
  scopes (§4).
- **#147 reframe** — feedstock quality + title cleanliness, decoupled from
  promotion (§5).

## Relationships
- Builds on #155 (boot injection), #156 (unified recall), #146 (clustering +
  confidence), #104 (ontology roles).
- #157 (promotion detection → SurrealDB) is orthogonal (that's the Engram-HTTP
  reorient); this note is about *what* rises and *how* it's consumed.
