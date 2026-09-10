## Why

Two worker-lifecycle defects waste resources and behave non-deterministically:

1. **Workers linger after their work is done.** When a process (SWE) worker's MR is merged outside the daemon's own auto-merge (human merge, or `auto_merge` off), nothing reaps it: the merge path skips process workers (`mrs.go:131-135`), `handlePostMerge` never kills, and orphan-recovery only writes a journal marker. The worker is left to "self-terminate," which is non-deterministic — it typically idles at the Pi prompt forever, holding a tmux session and compute.
2. **Orphan-recovery respawns eagerly.** Every tick it resurrects *any* run that is unfinished + MR-not-done + not-alive, even when no event is pending for it. A worker that won't be needed for hours or days is kept warm, burning a Pi process for nothing.

Both are fixable because the daemon already computes the exact terminal condition and already delivers events over a durable inbox — but the current logic is split across three disagreeing code paths, and respawn is poll-driven instead of event-driven.

## What Changes

- **Single deterministic reap authority.** The daemon becomes the one place that kills a worker, and it does so exactly when work is terminally done: MR merged **and** (post-merge main pipeline succeeded **or** post-merge monitoring is disabled for the vigne). Applies uniformly to process and non-process workers. Guard: if `main_pipeline_failed` (or any actionable follow-up) was dispatched, the worker is not reaped — it has work. **BREAKING** (behavioral): removes the "process workers self-terminate" assumption in `mrs.go`.
- **Event-driven (lazy) respawn.** Respawn a dead **local** worker only when there is a pending event for it:
  - *Primary — spawn-on-the-edge:* at dispatch time (`dispatchToWorkerWithType`), ensure the target worker is alive before/at delivery; if dead, spawn it, then the durable inbox message is caught up on boot.
  - *Backstop — inbox-gated orphan-recovery:* orphan-recovery respawns only when the worker's durable inbox consumer reports pending messages (`num_pending`/`num_ack_pending > 0`), replacing today's "respawn any unfinished run." No pending event ⇒ leave dead and reap stale state.
- **Location-agnostic liveness.** Replace the `/proc`-only authority with a signal that works for remote workers too: a NATS-based liveness probe (request/reply ping or heartbeat) is authoritative across hosts, with `/proc` (`liveness.LiveRunIDs`) retained only as a local fast-path.
- **Local/remote worker classification.** Workers register whether they are daemon-spawnable (local) or externally-scheduled (remote/HPC) in KV. Daemon-driven reap and respawn are scoped to **local** workers; remote workers (launched by their own scheduler over NATS) are never treated as orphaned-and-respawnable — they rely on the durable inbox to catch up when they return.

## Capabilities

### New Capabilities
- `worker-liveness`: A location-agnostic liveness model. Defines the authoritative liveness signal (NATS probe/heartbeat) that works for both local and remote workers, `/proc` as a local optimization, and the local-vs-remote classification the daemon uses to decide what it may reap or respawn.

### Modified Capabilities
- `worker`: Deterministic terminal reap replaces self-termination for process workers; workers answer the liveness probe / publish a heartbeat and record their local/remote classification at spawn.
- `event-flow`: Worker respawn becomes event-driven — spawn-on-the-edge at dispatch plus inbox-depth-gated orphan-recovery — and the single deterministic reap point is documented in the routing/lifecycle rules.

## Impact

- **Go daemon**: `internal/watcher/mrs.go` (single reap point in the merge / post-merge path; ensure-alive-then-deliver in `dispatchToWorkerWithType`), `internal/watcher/orphan_recovery.go` (inbox-gated lazy respawn, location-aware liveness, skip remote), `internal/liveness/` (NATS probe alongside `/proc`), `internal/pnats/` (liveness request/reply subject + consumer-info / pending-count helper), `cmd/aoc/cmd_spawn.go` (write local/remote marker to KV).
- **Worker extension**: `pi-extension/worker/index.ts` (respond to liveness probe or emit heartbeat; unchanged durable inbox already provides catch-up).
- **Behavioral / operational**: process workers are now killed by the daemon at terminal state (previously lingered); idle workers are no longer kept warm; remote runs are explicitly out of scope for daemon reap/respawn. No config schema change required; local/remote is derived from the existing standalone-launch path (`--vignoble-name`).
- **Specs**: new `worker-liveness`; deltas to `worker` and `event-flow`.
