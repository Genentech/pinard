---
title: The Semi-Deterministic Loop
weight: 10
group: Core Concepts
---

The semi-deterministic loop is Pinard's core idea. It is what turns a capable but
unpredictable LLM agent into a **reliable, resumable, auditable** unit of work.

## Deterministic control, non-deterministic work

A free-running agent is handed a goal and a pile of tools and left to figure out
the path. That is flexible but fragile: it wanders, repeats itself, can't be
resumed after a crash, and gives you no honest account of what it did.

A semi-deterministic loop inverts the balance. You define the **path** — an
explicit sequence of steps — as ordinary code. The model is only invoked *inside*
a step, to do the one bounded thing that genuinely needs judgment:

<figure class="doc-figure doc-figure--wide">
  <div class="doc-figure-visual">
    <img src="/images/docs/semi-deterministic-loop.jpg" alt="One six-stage execution track interrupted after stage three, with the first three journal cells completed and an arrow from the open fourth cell back to stage four.">
    <span class="doc-figure-label mustard" style="--x: 9%; --y: 8%;">1 · Code step</span>
    <span class="doc-figure-label terracotta" style="--x: 25%; --y: 8%;">2 · LLM turn</span>
    <span class="doc-figure-label mustard" style="--x: 40%; --y: 8%;">3 · Code step</span>
    <span class="doc-figure-label terracotta" style="--x: 61%; --y: 8%;">4 · LLM turn</span>
    <span class="doc-figure-label mustard" style="--x: 77%; --y: 8%;">5 · Code step</span>
    <span class="doc-figure-label terracotta" style="--x: 92%; --y: 8%;">6 · LLM turn</span>
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 51%;">Crash after step 3</span>
    <span class="doc-figure-label charcoal" style="--x: 18%; --y: 66%;">Journal: steps 1–3 complete</span>
    <span class="doc-figure-label mustard" style="--x: 61%; --y: 63%;">Resume at step 4</span>
  </div>
  <figcaption><strong>Fixed control, bounded judgment.</strong> The right half is not a different program: it is the unchanged remainder of the original six-step plan. After a crash, the journal skips completed steps 1–3 and resumes at open step 4.</figcaption>
  <ul class="doc-figure-legend" aria-label="Semi-deterministic loop legend">
    <li><span><strong>Code steps</strong> — deterministic code owns sequencing, branching, and stopping.</span></li>
    <li><span><strong>LLM turn</strong> — the model supplies bounded judgment only inside a declared step.</span></li>
    <li><span><strong>Journaled outcomes</strong> — completed tasks and breakpoint decisions survive interruption.</span></li>
    <li><span><strong>Crash → resume</strong> — replay skips completed work and continues at the next incomplete step.</span></li>
  </ul>
</figure>

The **control flow is deterministic** — the loop decides what runs next and when
to stop. The **work inside a step may be non-deterministic** — the model's turn.
Hence *semi-deterministic*.

## The babysitter

The component that runs a loop is the **babysitter**. A loop is defined by a
**babysitter process** — a `process.js` file that lists the steps and their logic.
The babysitter:

- drives the agent step by step, invoking the model only where the process says to;
- **journals** every step and its outcome to a per-run directory;
- **resumes** an interrupted run from that journal;
- enforces **gates** (breakpoints) where the loop pauses for a verdict or a human;
- records **verdicts and decisions** so the run is auditable after the fact.

A process lives with the code it operates on — baked into a repo at
`pinard/<process>/process.js` — so the loop is versioned alongside the thing it
automates. See [Authoring Processes](/docs/authoring-processes/) to write one.

## Runs, journals, and resume

Every execution of a process is a **run**, identified by a stable **run id**.
The babysitter writes a journal under a runs directory keyed by that id:

```
<runs-dir>/<run-id>/
  run.json          # the run's step-by-step state
  pi-session.jsonl  # the agent's session transcript
```

Because the run id is stable, re-invoking the process **resumes the same run** —
the engine rebuilds its state from the journal and continues from the next
incomplete step. A crashed agent, a rebooted host, or a multi-day pipeline all
pick up exactly where they left off, rather than starting over.

> Rebuilding state from the journal on resume is **routine** — the engine
> reconstructs and continues. It is not an error.

## Why it matters

| Free-running agent | Semi-deterministic loop |
|--------------------|-------------------------|
| Path decided by the model, per run | Path decided by code, every run |
| Non-resumable — a crash loses everything | Resumable from the journal |
| Opaque — "what did it do?" | Auditable — read the steps and verdicts |
| Unbounded — may wander or loop | Bounded — the loop decides when to stop |
| Hard to distribute reliably | A unit you can spawn, retry, and recover |

This is what makes it safe to run **fleets** of agents across many repos and
machines: each one is a bounded, recoverable loop rather than an open-ended chat.

## Where loops run

A loop is executed by a **vendangeur** (worker) — usually spawned by the daemon in
its own git worktree, but it can also run **standalone** on remote hardware (an
HPC node, a SLURM job) with no daemon at all. Either way it talks to the vignoble
over NATS and resumes from its journal. See
[Distributed & Remote Execution](/docs/remote-workers/).

## Next

- **[Authoring Processes](/docs/authoring-processes/)** — write your own loop.
- **[Architecture](/docs/architecture/)** — how the babysitter fits the engine.
- **[The SWE Process](/docs/swe-process/)** — the reference loop, end to end.
