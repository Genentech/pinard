---
title: Architecture
weight: 11
group: Core Concepts
---

Pinard's components communicate **exclusively via NATS JetStream** over WebSocket
(`wss://`). There is no direct HTTP between components — NATS is the single bus, which is
why an agent can run from any network (or another machine) with only outbound NATS.

## Components

| Component | Code | Runs as | Model |
|-----------|------|---------|-------|
| **Daemon** | `cmd/aoc/daemon.go` | self-supervising background process | — |
| **Régisseur** | `pi-extension/pinard/` | Pi extension, `conductor` tmux window `[régisseur]` | Opus |
| **Maître** | `pi-extension/pinard/` (`PINARD_PARCELLE` set) | Pi extension, one tmux window per parcelle | Opus |
| **Vendangeur** (worker) | `pi-extension/worker/` | spawned tmux session | Sonnet |
| **CLI** | `cmd/aoc/` | Go binary (`aoc`) | — |
| **Launcher** | `bin/pinard` | shell script | — |

The **daemon is always running**; the **régisseur/maîtres are optional** LLM layers. See
[Orchestration & Parcelles](/docs/orchestration/) for how the three conductor tiers relate.

## The daemon

The daemon is the engine. Started with `aoc daemon start`, it re-execs itself detached
(no systemd), writes `.state/daemon.pid`, and logs to `logs/aoc-daemon.log`. It runs all
watchers as goroutines on one persistent NATS connection and handles:

- **MR watching** — pipelines, reviews, approvals, auto-merge, post-merge monitoring
- **Issue watching** — assignee detection and auto-spawn (every assigned issue)
- **Schedule evaluation** — cron spawns with backfill
- **Direct dispatch** — actionable events go straight to worker inboxes (no conductor needed)
- **Liveness** — orphan recovery and maître window recovery

**Hot-reload** is in-process: a tick polls the mtimes of the `aoc` binary, `vignes.yaml`,
and `schedules.yaml`, and `exec`s itself in place on any change (same PID, same logs).

## Communication

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/architecture-nats-topology.jpg" alt="A hand-drawn topology with civilian vineyard planners and estate managers above a central NATS pipe, worker processes at its sides, and daemon watchers below it.">
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 4%;">Régisseur & maîtres</span>
    <span class="doc-figure-label terracotta" style="--x: 8%; --y: 40%;">Vendangeurs</span>
    <span class="doc-figure-label terracotta" style="--x: 92%; --y: 40%;">Vendangeurs</span>
    <span class="doc-figure-label mustard" style="--x: 50%; --y: 53%;">NATS JetStream</span>
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 77%;">Daemon</span>
    <span class="doc-figure-label charcoal" style="--x: 50%; --y: 96%;">Watchers & persisted state</span>
  </div>
  <figcaption><strong>One bus, no side channels.</strong> Every component communicates through NATS; the daemon owns the mechanical watchers and direct dispatch.</figcaption>
  <ul class="doc-figure-legend" aria-label="Architecture legend">
    <li><span><strong>Régisseur & maîtres</strong> — optional LLM conductors above the bus.</span></li>
    <li><span><strong>Vendangeurs</strong> — independent workers connected through NATS, never directly to one another.</span></li>
    <li><span><strong>NATS JetStream</strong> — durable inboxes and events use the central bus.</span></li>
    <li><span><strong>Daemon & watchers</strong> — issue, MR, schedule, dispatch, liveness, and state machinery.</span></li>
    <li><span><strong>Broken terracotta routes</strong> — ephemeral core-NATS traffic such as BTW and interrupt.</span></li>
  </ul>
</figure>

### Three channels (conductor → worker)

| Channel | Subject suffix | Transport | Purpose |
|---------|----------------|-----------|---------|
| **Main inbox** | `…inbox` | JetStream (durable) | Actionable work, queued until the turn ends |
| **BTW** | `…btw` | core NATS (real-time) | Parallel questions, immediate reply, no persistence |
| **Interrupt** | `…interrupt` | core NATS (real-time) | Cancel the current turn |

### Events (worker / watcher → conductor)

Actionable events are dispatched by the **daemon directly to the worker's inbox**; the
rest are informational (the conductor sees them for visibility). `ACK_REQUIRED` events
need a human/conductor acknowledgement.

| Event | Source | Dispatched to worker? |
|-------|--------|----------------------|
| `pipeline_failed` | MR watcher | **Yes** |
| `review_comment` | MR watcher | **Yes** |
| `main_pipeline_failed` | MR watcher | **Yes** |
| `tag_pipeline_failed` | MR watcher | **Yes** |
| `mr_merged` / `mr_closed` | MR watcher | No |
| `needs_approval` | MR watcher | No (ACK_REQUIRED) |
| `circuit_breaker` | MR watcher | No (ACK_REQUIRED) |
| `issues_new` / `issues_comment` | Issue watcher | No |
| `schedule_spawned/skipped/failed` | Scheduler | No (ACK_REQUIRED) |
| `agent_idle` / `session_ended` | Worker | No |

## NATS subjects

Agent subjects are **parcelle-scoped** — they carry a literal `parcelles.<parcelle>`
segment (a worker always has a parcelle, defaulting to its project). Vignoble-level
subjects (issues/schedules/notifications) are not parcelle-scoped.

```
pinard.<vignoble>.parcelles.<parcelle>.agents.<session>.events.<type>
pinard.<vignoble>.parcelles.<parcelle>.agents.<session>.inbox
pinard.<vignoble>.parcelles.<parcelle>.agents.<session>.btw
pinard.<vignoble>.parcelles.<parcelle>.agents.<session>.interrupt
pinard.<vignoble>.notifications
pinard.<vignoble>.issues.<new|comment>
pinard.<vignoble>.schedules.<name>.<status>
```

Subjects are built with centralized helpers (`internal/pnats/subjects.go`) — never
hand-formatted. A maître consumer filters to its own parcelle; the régisseur uses the
vignoble-level subjects plus a KV overview rather than the per-parcelle firehose.

## tmux topology

One tmux server per vignoble (socket `pinard-<vignoble>`). The `conductor` session holds
the `[régisseur]` window plus one window per active parcelle maître. Vendangeurs are flat
tmux sessions on the same server, named parcelle-first (`<parcelle>--<project>-<id>`) so
the session list is self-describing and filterable.

### Visual palette

The launcher applies a **role-colored tmux palette** on startup so each agent tier is
visually distinct:

| Role | Surface | Color |
|------|---------|-------|
| Régisseur | `conductor` session base bar | Trellis grey; status-left shows `🍇 <vignoble>` |
| Régisseur | `[régisseur]` tab | Burgundy — inactive: rosé text / grey; active: white on burgundy |
| Maître | Per-parcelle window tabs | Gold — inactive: gold text / grey; active: ink on gold |
| Vendangeur | Whole-session status bar | Leaf green, `🧺 vendangeur` label |

Session/window pickers (`ctrl+b s`, `ctrl+b w`, `ctrl+b f`) are rebound to a colored
`choose-tree` that tints entries by role (🍷 conductor session, 🎩 régisseur window,
🧑‍🌾 maître windows, 🧺 vendangeur sessions), making the multi-tier layout easy to
navigate at a glance.

## State

All state is persisted on every mutation (write-through, atomic rename, flock):

| File | Contents |
|------|----------|
| `.state/mr-watcher.yaml` | Watched MRs, pipeline counts, post-merge state |
| `.state/issue-watcher.yaml` | Tracked issues, last note IDs |
| `.state/scheduler-runs.yaml` | Last run timestamps per schedule |
| `.state/daemon.pid` | Daemon PID (liveness via signal 0) |

Conductor and maître sessions are resumable Pi sessions
(`.state/regisseur-session.jsonl`, `parcelles/<name>/session.jsonl`).
