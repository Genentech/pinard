---
title: Orchestration & Parcelles
weight: 12
group: Core Concepts
---

Pinard organizes work in three tiers and groups it into **parcelles**. This page explains
how the conductors relate and how a workstream persists over time.

## Three tiers

One vignoble = one **régisseur** + N **maîtres** (one per active parcelle) + M
**vendangeurs** (workers).

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/orchestration-parcelles.jpg" alt="A vineyard divided into three plots, each with one foreman and several harvest workers, overseen by an estate manager; three harvest streams blend into one output below.">
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 7%;">Vignoble</span>
    <span class="doc-figure-label mustard" style="--x: 14%; --y: 28%;">Parcelle</span>
    <span class="doc-figure-label mustard" style="--x: 20%; --y: 42%;">Maître</span>
    <span class="doc-figure-label terracotta" style="--x: 11%; --y: 55%;">Vendangeurs</span>
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 78%;">Régisseur</span>
    <span class="doc-figure-label terracotta" style="--x: 53%; --y: 94%;">Cuvée branch</span>
    <span class="doc-figure-label charcoal" style="--x: 82%; --y: 88%;"><code>main</code></span>
  </div>
  <figcaption><strong>Scoped autonomy.</strong> The régisseur sees the estate, each maître owns one parcelle, and vendangeurs execute bounded work within it.</figcaption>
  <ul class="doc-figure-legend" aria-label="Orchestration legend">
    <li><span><strong>Régisseur</strong> — vignoble-wide overview and general lane.</span></li>
    <li><span><strong>Maître</strong> — one autonomous conductor per active parcelle.</span></li>
    <li><span><strong>Vendangeurs</strong> — task workers scoped to their parcelle.</span></li>
    <li><span><strong>Parcelle</strong> — durable workstream identity, context, and run journals.</span></li>
    <li><span><strong>Cuvée branch → <code>main</code></strong> — several worker branches converge before one final MR.</span></li>
  </ul>
</figure>

### Régisseur

The estate manager. It runs in the `conductor` tmux session's `[régisseur]` window and
owns the **general lane** — the vignoble overview plus anything unparceled, untriaged, or
vignoble-level. It does **not** consume the per-parcelle event firehose; it works from a
KV overview (`list_parcelles`) plus the notifications, issues, and schedules streams. Its
session persists at `.state/regisseur-session.jsonl`.

### Maître

A **parcelle-scoped conductor** — one per parcelle. It consumes only its parcelle's agent
events, runs a conductor-grade model, and keeps a persistent, resumable session at
`parcelles/<name>/session.jsonl`. A maître **always runs autonomously** on its parcelle's
events — its durable JetStream consumer ingests them and steers the session whether or not
you are watching — and **attaching to its window** (via `ctrl+b` select-window, `aoc maitre
attach`, or `/parcelle`) lets you watch and steer it interactively. There is no mode switch
keyed on attach state: autonomy is always on; attaching simply adds your own input alongside
the autonomous event stream.

### Session memory

Cross-session recall is provided by **Engram** (the `mem_*` tools) — both the régisseur
and each maître can read and write persistent memory that survives individual sessions.
See [Memory & Recall](/docs/memory/) for details.

- **Attach** with `aoc maitre attach --parcelle <name>` (spawns the window if missing,
  then switches to it), the `/parcelle <name>` command, or the `attach_parcelle` tool.
- **Steer** a maître by typing in its tmux window.
- **Liveness** is the daemon's job: it ensures a window exists for every parcelle with
  live work while the `conductor` session is running.

### Vendangeur (worker)

The harvester — see [Overview](/docs/overview/) for the naming. Each vendangeur takes one
task in its own git worktree, opens an MR, and is reaped when it merges. Its full life is
covered in the [Merge Request Workflow](/docs/mr-workflow/).

## Parcelles

A **parcelle** (a plot within a vineyard) is a *named, persistent workstream* — the
missing layer between "the whole vignoble" and "one issue". It gives a body of work
identity, scope, and memory that survives individual workers and sessions.

- Created conversationally with the conductor ("let's work on semantic search").
- Persists across sessions, workers, and days.
- Groups related issues, runs, and context; many parcelles are active at once.
- A worker is always spawned into a parcelle (default: its project name).

### Storage

```
vignoble-<name>/parcelles/
  semantic-search/
    parcelle.yaml          # metadata: description, status, issues, cuvée branch
    runs/
      exo-cli-dev-42/      # a babysitter run journal, scoped to the parcelle
      exo-cli-dev-45/
  babysitter/
    parcelle.yaml
    runs/ …
```

Run journals live under the parcelle (durable — they survive worker crashes and worktree
cleanup), not under a throwaway worktree.

### `parcelle.yaml` schema

Create `parcelles/<name>/parcelle.yaml` in the vignoble root:

```yaml
name: <name>                    # parcelle identifier — must match the directory name
project: <vigne>                # primary vigne this workstream belongs to
status: active                  # active | archived
created: <YYYY-MM-DD>           # creation date
description: "..."              # human-readable summary of the workstream
target_branch: cuvee/<name>     # cuvée strategy: auto-spawned workers target this branch
issues:                         # highest-priority parcelle signal: issue IIDs in this workstream
  - 27
  - 28
spec: openspec/.../spec.md      # optional: pointer to the governing spec
epic: <number>                  # optional: GitLab epic number (group namespaces only)
```

**Field notes:**

| Field | Notes |
|-------|-------|
| `status: archived` | Hides the parcelle from the dashboard and stops orphan recovery from managing its workers |
| `target_branch` | Read by the issue watcher when auto-spawning; a `target:<branch>` label on an individual issue still takes priority |
| `issues` | The highest-priority input to parcelle resolution (see below) |
| `epic` | Requires a GitLab **group** namespace (Premium/Ultimate); not available for user-namespace projects |

### Parcelle resolution precedence

When the daemon resolves which parcelle an issue belongs to, it checks in order:

1. **`issues:` list in `parcelle.yaml`** — the issue IID appears explicitly
2. **`parcelle:<name>` label** — the issue carries a matching label
3. **Project default** — falls back to the project name as the bucket

### Creating a parcelle

1. Write `parcelles/<name>/parcelle.yaml` (schema above). **Do not skip this step** — without it there is no `target_branch` routing, no dashboard entry, and no first-class workstream identity.
2. Optionally add `parcelle:<name>` labels to issues — useful as a secondary signal.
3. Spawn the maître window: `aoc maitre attach --parcelle <name>` or `/parcelle <name>`.

## Cuvée branches

When several agents target the **same repo** at once, concurrent MRs to `main` fight over
CI. A **cuvée** is an intermediate branch that batches them:

1. `create_cuvee` makes a `cuvee/<name>` branch.
2. Spawn each agent with `--target-branch cuvee/<name>` (or add a `target:cuvee/<name>`
   label to an issue, since the daemon spawns assigned issues itself).
3. Each agent's MR auto-merges into the cuvée branch, not `main`.
4. `open_cuvee_mr` opens the single, final MR from the cuvée to `main`.

Use a cuvée for 2+ agents on one project; skip it for a single agent or agents on
different repos.

> **Auto-creation of cuvée branches:** If the `cuvee/<name>` branch does not yet exist
> on origin when a vendangeur spawns, `aoc spawn` creates it automatically from the
> project's default branch and pushes it. You no longer need to pre-create the branch
> with `create_cuvee` before spawning — just declare `target_branch: cuvee/<name>` in
> `parcelle.yaml` or pass `--target-branch cuvee/<name>` and the branch appears on
> demand. Non-`cuvee/` branches are **not** auto-created (a missing non-cuvée branch
> returns a clear error asking you to push it first).

## Context files

Instruction files layer from broad to specific and are loaded into conductor context (and
appended to the prompts of agents they apply to):

| File | Scope |
|------|-------|
| `PINARD.md` | Global conductor instructions (symlinked from the Pinard repo) |
| `VIGNOBLE.md` | Vignoble-wide rules and conventions (optional) |
| `vignes/<name>/VIGNE.md` | Per-vigne rules, appended to that vigne's agent prompts (optional) |

Write a `VIGNE.md` when you learn project-specific rules agents should always follow
(e.g. "run `make test` before pushing", "use conventional commits").
