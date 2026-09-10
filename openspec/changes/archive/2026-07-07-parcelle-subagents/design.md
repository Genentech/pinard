## Context

Today a vignoble runs one conductor Pi session that consumes a JetStream consumer filtered to `pinard.<vignoble>.agents.*.events.>` and injects every event into its single context via `sendUserMessage(..., {deliverAs})` (`pi-extension/pinard/index.ts`). A Pi session is one agent loop over one conversation tree, so all parcelles share one attention and one context — focusing on one workstream is polluted by the others.

Pinard already has most of the substrate: a `parcelle` concept (`parcelles/<name>/`, `parcelle.yaml`, `internal/dashboard/panel_parcelles.go`), `aoc spawn --parcelle` that writes `kvState["parcelle"]`, one tmux server per vignoble (`internal/session/tmux.go`, socket `pinard-<vignoble>`) with each worker as a flat session, and orphan-recovery/state patterns (`internal/watcher/`, `internal/state/`).

Constraint from the user: **one conductor per vignoble**, no extra split panes, must retain the ability to switch into any parcelle and interact, and must identify a worker's parcelle from a raw `tmux ls` / prefix+`f` picker.

## Goals / Non-Goals

**Goals:**
- One conductor per vignoble that oversees N parcelles in parallel, each in an isolated, persistent, resumable session.
- Per-parcelle event isolation at the source (no client-side firehose filtering in the human's view).
- Autonomous-when-unattended, interactive-when-attended per-parcelle overseers.
- A control-room dashboard that owns cross-parcelle overview + a general lane for unparceled/untriaged work.
- Worker tmux session names that are self-describing and filterable by parcelle.

**Non-Goals:**
- Changing worker behavior or the daemon→worker inbox dispatch path.
- True simultaneous LLM reasoning inside a single Pi session (impossible; parallelism comes from separate overseer processes).
- A single-pane multiplexer that mirrors another process's session (explicitly rejected in favor of tmux windows).
- Solving parcelle rename/merge automation (manual dir move for now).

## Decisions

### D1: Persistent per-parcelle overseer subagents, session keyed by parcelle id
One long-lived, resumable Pi session per parcelle. Session **state** persists at `parcelles/<name>/session.jsonl` (co-located with `parcelle.yaml`/`runs/`); the process may exit when idle and resume-on-demand via `--session`. Resume-if-exists mirrors the standalone-worker stable-RUN_ID + persistent runs-dir pattern (CLAUDE.md). The general/dashboard lane gets a reserved `parcelles/_general/session.jsonl`.
- **Why parcelle id as the key**: it is already the unique, fs-safe directory name within a vignoble; deterministic resume with zero new keyspace.
- **Alternative considered**: PID/session-name keyed files (current worker default) — rejected because it breaks deterministic resume for a long-lived overseer.

### D2: Control-room = tmux windows on the existing per-vignoble server
The conductor session holds windows `[dashboard, <overseer per parcelle>…]`. Attach = native tmux window switch to the real Pi TUI. Workers remain flat sessions on the same server.
- Natural 2-level view: `tmux ls` → dashboard + workers; `tmux list-windows -t <conductor>` → overseers.
- **Alternative considered**: single-pane in-conductor multiplexer (mirror + steer-chat) — rejected: high build cost, latency/fidelity risk. **Alternative considered**: a tmux server per (vignoble, parcelle) — rejected: multiplies sockets, forces `parcelle` into every `-L` call, complicates cross-parcelle overview, for isolation KV already provides.

### D3: Per-parcelle event scoping via a `parcelles` subject segment (DECIDED)
Each overseer owns a NATS consumer that receives only its parcelle's events; the dashboard subscribes only to a lightweight overview (counts/alerts) plus the vignoble-level subjects, never the per-parcelle firehose.

Agent/worker subjects gain a **literal `parcelles` segment** followed by the parcelle id:
```
pinard.<vignoble>.parcelles.<parcelle>.agents.<sid>.{events,inbox,btw,interrupt}
```
- **Why the literal `parcelles` token**: without it, `<parcelle>` would sit in the same slot as the fixed tokens `agents`/`issues`/`schedules`/`notifications`, so a parcelle named e.g. "issues" would collide. The constant namespaces parcelle-scoped subjects cleanly.
- **Agent events always have a parcelle**: a worker is always spawned with `--parcelle` (defaulting to the project), so everything under `agents.<sid>` is parcelle-scoped by construction. There is no dual parcelle/no-parcelle agent subject.
- **Parcelle-less events stay vignoble-level** and are the dashboard's domain, unchanged: `pinard.<vignoble>.issues.>`, `pinard.<vignoble>.schedules.>`, `pinard.<vignoble>.notifications`, daemon health.
- **`_general`** is reserved for the dashboard's own session, NOT a fake parcelle on the wire.
- **Consumer filter** per overseer: `pinard.<vignoble>.parcelles.<parcelle>.agents.*.events.>`. **Stream wildcard** migrates to `pinard.*.parcelles.*.agents.*.events.>` (daemon applies on connect).
- **Alternative considered (rejected)**: keep subjects flat and resolve `sid → parcelle` via KV per event — simpler wire but a KV read per event and no server-side filtering; Option A is the long-term-clean choice we're taking now.

### D3a: Subject migration
This is a subject restructuring, so publishers (workers, MR/issue/scheduler watchers), the worker's own channel subscriptions, the stream subject wildcards, and durable consumer filters all move to the `parcelles.<parcelle>` form together. Cut over as a versioned change: bump the stream/consumer names so old durables are not reused, and ensure workers derive their parcelle (already in KV / spawn args) when constructing subjects.

### D4: 3-step event routing with a general-lane fallback
Resolve each event to a destination: (1) explicit `parcelle:<name>` label / KV field → that overseer; (2) else project's default KindRepo bucket (default parcelle == vigne name) → that overseer; (3) else → dashboard general lane. The general lane is also where untriaged/vignoble-level events land and where new parcelles are born by promotion.

### D5: Worker session naming — parcelle-leading, vignoble-free, tmux-safe
New auto-generated name: `<parcelle>--<project>-<id><rand>` where `<id>` is the issue IID or time token and `<rand>` is the existing collision suffix. Drop the redundant `<vignoble>` token (the socket already scopes it). Add a tmux-safe sanitizer (forbid `.` and `:`, which are tmux target separators). Dashboard per-parcelle counts come from the authoritative KV `parcelle` field, not name parsing; the name tag is human/CLI ergonomics for `tmux ls` and prefix+`f`.
- Change is localized to name construction in `cmd/aoc/cmd_spawn.go`; `StopWorker`/`GetWorkerCwd`/orphan recovery/KV key off the name+socket and are unchanged.

### D6: Overseer built on the native interactive-session spawn path (DECIDED, v1)
An overseer is a **scoped conductor session**, physically the same kind of thing as a worker: a real, attachable, interactive Pi session in a tmux window. It is spawned via Pinard's **existing** session-spawn path (the one that already launches workers and the conductor) — parameterized for parcelle scope, not new subagent machinery.
- **Why not a subagent package for the overseer itself**: `pi-subagents` (and similar) run children **headless** (background async, interaction via `steer` + reading `events.jsonl`), which cannot provide the locked control-room UX of switching to the overseer's **live Pi TUI** in a tmux window. Reusing the native spawn path preserves that UX and requires no hand-rolled subagent code and no new dependency.
- **Attach** = native tmux window switch (full Pi TUI). **Steer** = a `send_message`-style delivery into that overseer's session, which the model already supports.
- **pi-subagents deferred to a delegation layer (future, optional)**: once an overseer wants to fan out bounded helpers (`scout`/`reviewer`/`researcher`) and collect results, adopt `pi-subagents` *inside* overseers/dashboard. That is where its headless-child model fits and where it "opens future opportunities." It is explicitly **not** a v1 dependency; deferring it avoids `PI_SUBAGENT_PI_BINARY` wiring + dist bundling for now.
- **Alternative considered (rejected for v1)**: overseer = pi-subagents headless child with a dashboard-rendered view + steer instead of a live TUI — rejected because it walks back the control-room attach decision.

## Risks / Trade-offs

- **No true parallel reasoning within one session** → parallelism is delivered by separate overseer processes; the dashboard stays a light router. Accepted by design.
- **Cost: N warm conductor-grade sessions per vignoble** → keep active-parcelle overseers warm (v1); idle-exit is a deferred optimization (see below). Only archived/inactive parcelles have no process.
- **Concurrent state mutation** (dashboard + overseers touching KV/session/MR state) → Pinard state layer is flock + atomic + reload-before-update; verify the KV path specifically for overseer spawn/resume races.
- **Subject migration (D3a)** is a coordinated cutover across publishers, worker channels, stream wildcards, and durable consumers → version the stream/consumer names so old durables aren't reused; land publisher + consumer changes together.
- **Spec accuracy** → deltas written topology-accurately to match the tmux-based runtime.
- **General-lane overflow** (everything unrouted piles up) → bound it and surface counts in the dashboard rail; promotion to a parcelle is the pressure valve.
- **tmux name collisions / illegal chars** → keep the existing collision suffix and add the sanitizer before any name reaches tmux.

## Resolved Decisions

- **D6 build approach**: overseer = native interactive-session spawn (v1); `pi-subagents` deferred to an optional delegation layer inside overseers.
- **D3 event scoping**: Option A — `pinard.<vignoble>.parcelles.<parcelle>.agents.<sid>.…` with the literal `parcelles` segment.
- **Overseer liveness owner**: the **daemon** (extend orphan recovery to overseers). The dashboard may also resume an overseer lazily on attach, but the daemon is the authority.
- **Idle-exit**: **deferred.** v1 keeps active-parcelle overseers warm; only archived/inactive parcelles have no process. Revisit idle-exit (with a timeout + pending-gate exemption) only if a vignoble routinely runs many simultaneously-active parcelles.

## Open Questions

- Idle-exit timeout + exemptions — deferred; revisit only under many-active-parcelle load.
- Whether the dashboard's own general lane needs to consume any `parcelles._general.*` agent subjects, or purely the vignoble-level subjects (leaning: vignoble-level only; `_general` is for its own session state).
