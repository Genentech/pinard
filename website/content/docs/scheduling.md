---
title: Scheduling
weight: 50
group: Operations
---

Pinard can spawn agents on a cron schedule — for nightly syncs, periodic checks, or any
recurring task. The scheduler runs inside the daemon (every 60s) and reads
`schedules.yaml`.

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/scheduling-dispatch-v3.jpg" style="aspect-ratio: 32 / 15;" alt="A single sketched scheduling sequence: a clock dispatches through the daemon to a vendangeur and vigne task, then outcomes notify the conductor.">
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 11%; --y: 83%;">Schedule</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 34%; --y: 83%;">Daemon</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 50%; --y: 83%;">Vendangeur</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 67%; --y: 83%;">Vigne task</span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 82%; --y: 83%;">Outcome event</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 94%; --y: 83%;">Conductor</span>
  </div>
  <figcaption><strong>The normal dispatch path.</strong> The daemon evaluates the schedule, starts a vendangeur in the vigne, and reports the outcome to the conductor. Backfill behavior is described below.</figcaption>
  <ul class="doc-figure-legend" aria-label="Scheduling and backfill">
    <li><span class="doc-figure-key">1</span><span><strong>Normal dispatch</strong> — the daemon evaluates the cron entry and spawns a worker.</span></li>
    <li><span class="doc-figure-key terracotta">2</span><span><strong>Backfill</strong> — a missed evaluation is found from the last-run ledger after restart.</span></li>
    <li><span class="doc-figure-key charcoal">B</span><span><strong>Branch</strong> — the worker starts from the vigne's configured default branch.</span></li>
    <li><span class="doc-figure-key">!</span><span><strong>Events</strong> — spawned, skipped, and failed outcomes require conductor acknowledgement.</span></li>
  </ul>
</figure>

## Define a schedule

```bash
aoc add schedule nightly-sync \
  --project mnemosyne \
  --cron "0 2 * * *" \
  --prompt "Run the sync task and open an MR if anything changed"
```

This appends to `schedules.yaml`:

```yaml
schedules:
  nightly-sync:
    project: mnemosyne
    cron: "0 2 * * *"
    prompt: "Run the sync task and open an MR if anything changed"
    once: false
```

| Field | Meaning |
|-------|---------|
| `project` | Vigne to spawn the agent in |
| `cron` | Standard 5-field cron expression |
| `prompt` | Task prompt for the spawned agent |
| `once` | If true, the schedule disables itself after its first successful run |

## Manage schedules

```bash
aoc list-schedules            # schedules and their last run times
aoc unschedule --name nightly-sync
```

You can also edit `schedules.yaml` directly — the daemon hot-reloads it.

## Branch selection

Scheduled spawns base their worktree on the **vigne's default branch** (the same rule the
issue watcher uses). If a project's default branch isn't `main`, scheduled runs still
start from the right place — no extra configuration needed.

## Backfill

The scheduler tracks the last run time per schedule in `.state/scheduler-runs.yaml`. If
the daemon was down across a scheduled tick, it evaluates whether the run was missed and
backfills rather than silently skipping.

## Events

Each evaluation emits a scheduler event to the conductor (`schedule_spawned`,
`schedule_skipped`, or `schedule_failed`) as `ACK_REQUIRED`, so nothing runs unnoticed.
