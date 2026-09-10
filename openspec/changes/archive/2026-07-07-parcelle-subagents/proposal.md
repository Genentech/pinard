## Why

A single conductor per vignoble receives every parcelle's events into one context, so focusing on one workstream is polluted by unrelated noise from all the others. We want one conductor per vignoble that can **oversee multiple parcelles in parallel** — each workstream reasoned about in its own isolated, persistent, resumable session — while the human switches between them and the actual worker execution keeps running underneath.

## What Changes

- **Introduce persistent per-parcelle overseer subagents.** One long-lived, resumable Pi session per parcelle (conductor-grade model, not downgraded). Autonomous when unattended; interactive (`steer`) when attended. Session **state** persists (`parcelles/<name>/session.jsonl`, resume-if-exists); the process may exit when idle and resume on demand — mirroring the standalone-worker stable-RUN_ID + persistent runs-dir pattern.
- **Turn the top-level conductor into a dashboard/control-room** that owns the cross-parcelle overview plus a first-class **general lane** for unparceled, vignoble-level, and untriaged work (also where new parcelles are born by promotion). It subscribes only to a lightweight overview, not the per-parcelle firehose.
- **Control-room tmux UX**: on the existing per-vignoble tmux server, the conductor session holds windows `[dashboard, parcelle-A overseer, parcelle-B overseer, …]`. "Switch to a parcelle" = native tmux window switch to the real Pi TUI (full fidelity, no mirroring, one full-screen view, no split panes).
- **Scope event delivery per parcelle at the source.** Each overseer owns a NATS consumer filtered to only its parcelle (via a parcelle segment in the subject hierarchy or `sid→parcelle` KV resolution). Events that map to no parcelle route to the general lane.
- **3-step event routing**: explicit `parcelle:<name>` label / KV field → project's default KindRepo bucket → dashboard general lane.
- **Encode parcelle in worker tmux session names** (leading token, e.g. `<parcelle>--<project>-<id><rand>`) so `tmux ls` and the prefix+`f` session picker are self-describing and filterable. Drops the now-redundant `<vignoble>` token (the socket already scopes it); keeps the existing collision suffix; adds a tmux-safe name sanitizer (forbid `.` and `:`).
- **Workers themselves are unchanged**: they remain flat tmux sessions on the same per-vignoble server, and the daemon still dispatches actionable events directly to worker inboxes.

## Capabilities

### New Capabilities
- `parcelle-subagents`: lifecycle, isolation, and persistence of per-parcelle overseer subagents — spawn/resume by parcelle id, per-parcelle NATS scoping, persistent session files, idle-exit/resume-on-demand, and orphan recovery for overseers.
- `conductor-control-room`: the dashboard/control-room UX and behavior — cross-parcelle overview, the general lane for unparceled/untriaged work, tmux window topology, and switching/attach semantics.

### Modified Capabilities
- `event-flow`: event routing gains parcelle resolution (label → default bucket → general lane) and per-parcelle subject scoping; the conductor no longer receives the undifferentiated per-parcelle firehose.
- `conductor`: the conductor's role changes from a single all-events context to a dashboard/router that oversees per-parcelle overseers; spawning/steering/attaching overseers is added.
- `aoc`: `aoc spawn` worker session naming changes to a parcelle-leading, vignoble-free, tmux-safe scheme; parcelle-scoped session file resolution is added.

## Impact

- **Code**: `cmd/aoc/cmd_spawn.go` (worker naming, parcelle session resolution, KV — already writes `parcelle`); `internal/session/tmux.go` (topology assumptions, sanitizer); `pi-extension/pinard/index.ts` (NATS consumers, event routing, dashboard vs overseer modes); `internal/dashboard/panel_parcelles.go` (reuse `ParcellesPanel` for the rail); `internal/watcher/` (issue→parcelle resolution, orphan recovery extended to overseers); `bin/pinard` launcher (dashboard vs overseer startup).
- **Specs**: `openspec/specs/event-flow`, `openspec/specs/conductor`, `openspec/specs/aoc`; new specs under `parcelle-subagents` and `conductor-control-room`. Related: `data-pipeline/parcelle.md`.
- **NATS subjects**: optional restructuring to include a `<parcelle>` segment (`pinard.<vignoble>.<parcelle>.agents.…`); otherwise KV-based `sid→parcelle` resolution. Affects stream subject wildcards and durable consumer filters.
- **Distribution**: only if the build fork chooses an external subagent package (`PI_SUBAGENT_PI_BINARY` wiring + dist bundling); the native-machinery option has no dist impact.
- **Open build decision (see design.md)**: adopt an existing subagent package (e.g. pi-subagents) vs. extend Pinard's own spawn+tmux+state machinery. Lean: extend native machinery.
