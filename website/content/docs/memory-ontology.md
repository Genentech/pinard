---
title: The Ontology & Portable Memory
weight: 32
group: Memory
---

> **Status:** 🔭 designed. This page describes the memory layer Pinard is building
> toward (the `memory-layer` change). The shipped memory system is
> [Memory & Recall](/docs/memory/).

For a fleet's knowledge to be *queryable* and *portable*, it has to be **typed**.
Pinard types memory with a **layered ontology** and ships it as **versioned,
portable subsets**.

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/ontology-portable-memory.jpg" alt="A sketched grapevine with a stable shared trunk, distinct domain branches, young learned shoots, one human-reviewed graft toward the core, and a three-compartment portable case holding runtime, process, and memory.">
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 36%; --y: 84%;">L1 · <code>pinard-core</code></span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 20%; --y: 46%;">L2 · repository ontology</span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 45%; --y: 52%;">Learned types</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 69%; --y: 49%;">Human-reviewed promotion</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 79%; --y: 89%;">Portable agent · one versioned unit</span>
  </div>
  <figcaption><strong>Stable core, specialized branches, deliberate promotion.</strong> Repository ontologies extend the shared operational vocabulary; learned types remain local unless recurrence and human review justify promotion.</figcaption>
  <ul class="doc-figure-legend" aria-label="Layered ontology and portable memory">
    <li><span class="doc-figure-key charcoal">L1</span><span><strong><code>pinard-core</code></strong> — the small, stable operational trunk shared by every agent.</span></li>
    <li><span class="doc-figure-key">L2</span><span><strong>Repository ontology</strong> — domain concepts branch from and subclass the core.</span></li>
    <li><span class="doc-figure-key terracotta">↑</span><span><strong>Learned → promoted</strong> — only a reviewed, proven domain type is grafted toward core.</span></li>
    <li><span class="doc-figure-key charcoal">▣</span><span><strong>Portable agent</strong> — runtime, <code>process.js</code>, and scoped memory are versioned together.</span></li>
  </ul>
</figure>

## Why layered

A single flat ontology forces a false choice: either it is generic and useless, or
it bakes one domain's specifics (say, GWAS/HPC terms) into what should be shared by
everyone. Pinard splits it into two layers instead.

### Layer 1 — `pinard-core`

Repo-agnostic, agent-operational concepts that mirror the primitives of a
[semi-deterministic loop](/docs/semi-deterministic-loop/). Small and stable,
versioned centrally in Pinard:

- **Entities:** `Task` / `Step`, `Verdict` / `Decision`, `Gate` (a breakpoint),
  `Action`, `Diagnosis`, `LogPattern`, `EnvironmentCondition`, `Artifact`.
- **Edges:** `DependsOn`, `Produces`, `Consumes`, `IndicatesProblem`, `ResolvedBy`,
  `RequiresCondition`, `TriggersDecision`.

### Layer 2 — per-repo domain

Each repository defines its own ontology that **subclasses core** and lives next to
its `process.js`. For example, a GWAS pipeline repo might define:

- `SlurmJob` *is-a* `Task`/execution
- `ShardThresholdDecision` *is-a* `Decision`
- `GWASStudy` *is-a* `Artifact`
- `ProvenanceRecord`

Granularity is **per-repo by default** (a per-process override only where a process
genuinely diverges; per-agent is too granular). A `data-pipeline` mid-layer between
core and domain is **deferred but intended**.

## Lifecycle: prescribed → learned → promoted

Types are not frozen. They move through a lifecycle:

- **prescribed** — declared up front (core, or a repo's domain);
- **learned** — new types emerge during `/teaching` sessions;
- **promoted** — a proven domain type is elevated toward core.

Promotion **domain → core is human-gated** (a git PR), and a `suppressed_types` list
retires types that stop earning their place. This is the same recurrence-plus-review
pattern used for [rule and scope promotion](/docs/memory-curation/#scope--promotion).

## Portable memory subsets

This is the payoff of a single, embeddable store of record. The central memory
(SurrealDB server) can be **subset** — scoped to a repo or pipeline — into a
specialized **embedded** SurrealDB file that an agent loads locally.

That makes a **pinard agent** a self-contained, reproducible artifact:

```
 pinard agent  =  harness  +  babysitter process  +  memory subset
                  (the runtime) (the loop, process.js) (embedded, scoped store)
```

All three are **versioned together** (e.g. in ExoHub), so an agent-centric data
pipeline is **reproducible and auditable**: you can rebuild exactly the agent, its
loop, and the knowledge it had at a given version.

### Version-stamping

Every portable subset **version-stamps both** the `pinard-core` ontology version and
the domain ontology version it was extracted under. A migration policy governs what
happens when core or domain versions change under an already-shipped subset — new,
load-bearing scope that portability introduces.

## Next

- **[Teaching & Curation](/docs/memory-curation/)** — how knowledge is captured, curated, and promoted.
- **[The Layered Memory Architecture](/docs/memory-architecture/)** — where the ontology sits in the stack.
