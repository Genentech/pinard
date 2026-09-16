---
title: Overview
weight: 1
group: Introduction
---

Pinard is a **distributed engine for orchestrating fleets of LLM agents through
semi-deterministic loops** — deterministic runbooks that drive non-deterministic
agents, coordinated over NATS across many repositories and machines.

One conductor. Many agents. One harvest.

## The idea

A raw LLM agent is powerful but unpredictable: it wanders, forgets, and can't be
resumed. Pinard wraps each agent in a **semi-deterministic loop** — a defined
sequence of steps where the *control flow is deterministic code* and only the
*work inside a step* is left to the model. The result is agent work that is

- **reliable** — the loop decides what happens next, not a hopeful prompt;
- **resumable** — every step is journaled, so a crashed run picks up where it left off;
- **auditable** — you can read exactly which steps ran and why;
- **distributed** — loops run as agents spread across repos and machines, coordinated over NATS.

Three pillars hold it up:

> **Deterministic control** (the loop) · **Non-deterministic agents** (the model) · **Persistent memory** (recall across sessions)

Automating code review — turning GitLab issues into merged MRs — is just **one
built-in loop**. It is an application of the engine, not the engine itself.

## The mental model

Pinard borrows its vocabulary from winemaking. Everything below is a role or a
place in the estate:

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/overview-mental-model.jpg" alt="A sketched vineyard estate divided into work plots, with an estate manager, plot foremen, harvest workers, and harvest streams converging in a blending vessel.">
    <span class="doc-figure-region" style="--x: 5%; --y: 19%; --w: 39%; --h: 31%;" aria-hidden="true"></span>
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 4%;">Vignoble</span>
    <span class="doc-figure-label mustard" style="--x: 18%; --y: 25%;">Vigne · repository</span>
    <span class="doc-figure-label mustard" style="--x: 23%; --y: 51%;">Parcelle · outlined workstream</span>
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 47%;">Régisseur</span>
    <span class="doc-figure-label mustard" style="--x: 70%; --y: 47%;">Maître</span>
    <span class="doc-figure-label terracotta" style="--x: 83%; --y: 59%;">Vendangeurs · workers</span>
    <span class="doc-figure-label terracotta" style="--x: 51%; --y: 95%;">Cuvée</span>
    <span class="doc-figure-label charcoal" style="--x: 82%; --y: 95%;"><code>main</code></span>
  </div>
  <figcaption><strong>The Pinard estate.</strong> The vineyard vocabulary describes real boundaries of scope, responsibility, and work.</figcaption>
  <ul class="doc-figure-legend" aria-label="Pinard estate legend">
    <li><span><strong>Vignoble / vigne / parcelle</strong> — workspace, repository, and persistent workstream.</span></li>
    <li><span><strong>Régisseur / maître / vendangeur</strong> — estate conductor, workstream conductor, and task worker.</span></li>
    <li><span><strong>Cuvée → <code>main</code></strong> — several worker branches blend before one final merge.</span></li>
  </ul>
</figure>

| Term | Meaning |
|------|---------|
| **Vignoble** | A workspace (an "estate") — one directory that groups the repos and workstreams you orchestrate. |
| **Vigne** | A single repository ("vine") registered in the vignoble. |
| **Parcelle** | A named, persistent *workstream* — a plot of related work that spans many tasks, agents, and days. |
| **Cuvée** | An intermediate branch that batches several agents' outputs before they reach `main`. |
| **Régisseur** | The estate manager — the top-level conductor for the whole vignoble. |
| **Maître** | A parcelle-scoped conductor — one per active parcelle. |
| **Vendangeur** 🧺 | A *harvester* — a worker agent that runs one loop to completion, then is reaped. |

> **A note on "worker" vs "vendangeur".** User-facing surfaces (dashboards, status
> lines, tool labels) call the harvester a **vendangeur**. The **code** keeps the
> name **worker** — CLI flags (`--worker`), env vars, tool names (`list_workers`),
> config keys. When these docs describe CLI or code they say *worker*; when they
> describe what you see they say *vendangeur*.

## The pieces

Pinard is a handful of cooperating processes that talk **exclusively over NATS
JetStream** (`wss://`) — never direct HTTP between components.

- **Daemon** (`aoc daemon`) — the always-on engine. Watches events, evaluates
  schedules, auto-spawns agents, dispatches work to running loops, and owns the
  per-vignoble memory serve. It runs **without any LLM** and is the part that must
  always be up.
- **Régisseur & maîtres** — optional, LLM-powered conductors (Opus) that give you a
  conversational way to steer. The system functions without them; they add
  orchestration and interaction.
- **Vendangeurs (workers)** — agents (Sonnet) that each run one semi-deterministic
  loop — often in an isolated git worktree — and are cleaned up when done.
- **CLI** (`aoc`) — the Go binary that scaffolds vignobles, spawns agents, runs the
  daemon, and exposes everything else.

## How work flows

At the core, every unit of work is a **loop** run by an agent:

1. A trigger arrives — a schedule fires, an event is published, or you (or a
   conductor) start a task.
2. The daemon **spawns a vendangeur** running the appropriate **babysitter process**
   (the loop definition) with a stable *run id*.
3. The loop advances **step by step** — each step is deterministic code or a bounded
   LLM turn — **journaling** its state as it goes.
4. If the agent crashes or the host reboots, the run **resumes** from its journal.
5. What the agent learns is written to **memory**, so the fleet improves over time.
6. When the loop completes, the vendangeur is **reaped**.

The [SWE process](/docs/swe-process/) is the reference loop: *issue → change → MR →
merge*. But you write your own loops for whatever your fleet does.

## Where to go next

- **[The Semi-Deterministic Loop](/docs/semi-deterministic-loop/)** — the core concept.
- **[Getting Started](/docs/getting-started/)** — install and run your first loop.
- **[Architecture](/docs/architecture/)** — the engine internals.
- **[Memory & Recall](/docs/memory/)** — how the fleet remembers and improves.
