---
title: The Layered Memory Architecture
weight: 31
group: Memory
---

> **Status legend:** ✅ shipped · 🔭 designed · 🧪 spike (being de-risked). The
> engram capture-and-store surface described in [Memory & Recall](/docs/memory/) is
> shipped today. The layered architecture on this page is the **design** Pinard is
> building toward; treat 🔭 items as roadmap, not current behavior.

The memory layer is organized as a stack. Each layer has one job, and the layers
above depend only on the interface below them — so any single layer can be swapped
without breaking agents.

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/memory-layered-architecture.jpg" alt="A cellar cutaway with one rooftop scope landscape and exactly six interior floors: curation, ontology, knowledge graph, recall, one store-of-record floor, and capture.">
    <span class="doc-figure-label mustard" style="--x: 50%; --y: 7%;">L6 · Scope & promotion</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 10%; --y: 26%;">L5 · Curation & wiki</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 10%; --y: 39%;">L4 · Ontology</span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 10%; --y: 52%;">L3 · Knowledge graph</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 10%; --y: 65%;">L2 · Recall</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 10%; --y: 78%;">L1 · Store of record</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 10%; --y: 91%;">L0 · Capture</span>
    <span class="doc-figure-label terracotta" style="--x: 84%; --y: 54%;">Portability & embeddings</span>
  </div>
  <figcaption><strong>The memory cellar, bottom to top.</strong> Each level has one responsibility and exposes an interface to the level above. The side mechanisms represent portability and embeddings across the stack.</figcaption>
  <ol class="doc-figure-legend doc-figure-legend--stack" aria-label="Memory architecture layers">
    <li><span class="doc-figure-key">L0</span><span><strong>Capture ✅</strong> — curated lessons, teaching episodes, and passive observations enter through Engram.</span></li>
    <li><span class="doc-figure-key charcoal">L1</span><span><strong>Store of record 🔭</strong> — documents, graph edges, and vectors share one SurrealDB store.</span></li>
    <li><span class="doc-figure-key">L2</span><span><strong>Recall ✅</strong> — typed query/fetch intent and compact boot injection.</span></li>
    <li><span class="doc-figure-key terracotta">L3</span><span><strong>Knowledge graph 🧪</strong> — temporal graph value is evaluated before commitment.</span></li>
    <li><span class="doc-figure-key charcoal">L4</span><span><strong>Ontology 🔭</strong> — stable <code>pinard-core</code> plus specialized repository domains.</span></li>
    <li><span class="doc-figure-key">L5</span><span><strong>Curation & wiki ✅</strong> — a git-tracked, human-readable OKF artifact synchronized with the store.</span></li>
    <li><span class="doc-figure-key">L6</span><span><strong>Scope & promotion ✅</strong> — curated knowledge rises from vigne to vignoble to global.</span></li>
  </ol>
</figure>

The design through-line is **signal over noise**: capture *curated* knowledge
instead of dumping transcripts, keep it in **one multi-model store** instead of a
zoo of databases, and treat memory as a **versioned, portable artifact** you can
ship with an agent.

## Layer 0 · Capture — Engram *(✅ shipped)*

Knowledge enters through **Engram**, not raw transcripts. Three inputs:

- **`/lesson <text>`** ✅ — a one-shot pinned fact or rule, high-confidence and
  eligible for promotion (e.g. *"always use a `fix:`/`feat:` commit prefix"*). Available
  in conductor and vendangeur.
- **`/teaching`** ✅ — a *mode*: while active (with a visible status-line indicator,
  auto-off at session end) the session is captured as a `teaching-episode`. Supports
  retroactive capture via `--all` or `--from <duration>`. Conductor only.
- **Passive capture** ✅ — session summaries and curated `mem_save` observations, so
  lessons are never purely manual.

Using Engram as the write path removes most extraction noise: it already produces
**typed, scoped, curated** observations.

## Layer 1 · Store of record — SurrealDB 🔭

A single **multi-model** store replaces the original zoo (FalkorDB + Qdrant +
git-YAML). SurrealDB holds **documents**, **graph edges** (`RELATE`), and
**vectors** (HNSW) in one engine, and runs **either** as a central server **or** as
an embedded, file-based database — the duality that makes portable subsets possible
(see below). It is a single static binary, which fits the HPC "no Docker"
constraint. Agents never touch it directly; all access is through Layer 2.

## Layer 2 · Recall — a typed intent API 🔭

The memory service exposes a small, stable API over NATS request-reply — **not** raw
queries — so the store stays swappable:

- **`recall`** — semantic / vector neighbors,
- **`lookup`** — lexical / full-text,
- **`trace`** — knowledge-graph traversal.

It is **fail-open** with a ~3s timeout: if memory is slow or down, the agent
proceeds without it rather than blocking. Humans get a separate direct read-only
path for exploration.

## Layer 3 · Knowledge graph — Graphiti, time-boxed 🧪

A temporal knowledge graph gives two hard things cheaply: **bi-temporal validity**
(facts expire) and **LLM entity/edge extraction**. Rather than commit to it
permanently, Pinard runs **Graphiti (on FalkorDB) as an evaluation sidecar**, fed
one-way from the store of record (`SurrealDB → jsonl → Graphiti`). A **Phase-2
decision** then picks one of: keep Graphiti, build native temporal-KG in SurrealDB,
or adopt **Spectron** (an upcoming Graphiti-like temporal KG *on* SurrealDB) — the
preferred long-term slot because it needs no store migration.

## Layer 4 · Ontology — layered 🔭

Knowledge is typed by a **two-layer ontology**: a small, stable **`pinard-core`**
(agent-operational concepts that mirror babysitter primitives) plus a **per-repo
domain** layer that subclasses it. See [The Ontology](/docs/memory-ontology/).

## Layer 5 · Curation & wiki ✅

A self-evolving **git-tracked OKF wiki ⇄ SurrealDB** turns the typed memory graph
into curated, human-readable pages, with an **ontology gardener** that proposes
structural extensions via human-reviewed MRs. See
[Teaching & Curation](/docs/memory-curation/).

## Layer 6 · Scope & promotion ✅

Knowledge is stored at the finest grain (a vigne) and **rolled up** — vigne →
vignoble → global — with curate-on-promote ensuring only synthesized wiki_doc entries
rise (never raw entities). Vignoble-shared results sync to `wiki/_shared/` for
human visibility. See [Scope & Promotion](/docs/memory-curation/#scope--promotion).

## Cross-cutting

- **Embeddings — Rosetta** 🔭: an OpenAI-compatible endpoint
  (`qwen3-emb-0.6b`, **1024-dim**). The same endpoint embeds both writes and queries,
  guaranteeing vector comparability, with no token/login dance.
- **Portability** 🔭: the central store can be **subset** by scope into an embedded
  SurrealDB file — so a *pinard agent = harness + babysitter process + memory*, all
  three versioned together. See [The Ontology](/docs/memory-ontology/#portable-memory-subsets).

## Status & de-risking

**Shipped (Layers 0, 5, 6):** the `WikiCurator` (outbound, per-vigne namespacing,
LLM-synthesized summaries), `WikiSyncer` (inbound), `OntologyGardener`, the vignoble
OKF bundle scaffold, `/lesson`, `/teaching` (with retroactive modes), curate-on-promote
(only wiki rises, vignoble-shared sync-out), and boot injection v2 (compact manifest
with drill-down via `recall(fetch=<ref>)`) are all live.

The remaining in-flight work is two 🧪 spikes: **(1)** Engram → SurrealDB curated
ingestion and recall quality on 1024-d vectors (Layer 1 as the full store of record),
and **(2)** SurrealDB → jsonl → Graphiti, to judge the temporal-KG value before
committing (Layer 3). Until those land, the shipped engram path
([Memory & Recall](/docs/memory/)) remains the primary agent-facing write path.
