## Context

Worker termination and resurrection are split across code paths that disagree:

- `mrs.go:126-135` (observed merge): kills only non-process workers (`if processName == "" { stopSession }`); process workers are left to "self-terminate."
- `mrs.go:655` (`tryAutoMerge`): kills unconditionally, but only when the daemon itself performs the merge.
- `handlePostMerge` (`mrs.go:672-709`): monitors the main pipeline but never kills; after 10 checks it only deletes the MR-watch entry, not the tmux session/KV.
- `orphan_recovery.go`: `markRunCompleted` writes a `RUN_FAILED` journal marker but never kills; `Run()` respawns *any* unfinished + MR-not-done + not-alive run every tick (eager).

Liveness today is `/proc`-only (`liveness.LiveRunIDs`), chosen deliberately for authority (commit `93cf61e`). That is correct for local workers but blind to **remote** workers, which per `CLAUDE.md` run standalone on other hosts (HPC/example Singularity, launched with `--vignoble-name`, talking to the vignoble purely over NATS). The daemon can neither `/proc`-check nor spawn a remote worker.

The durable inbox already exists: `pi-extension/worker/index.ts:159-167` creates a JetStream consumer `worker-{AGENT_ID}` with `deliver_policy: "all"`, and the daemon dispatches actionable events to it regardless of worker liveness. A message published to a dead worker's inbox is therefore retained and delivered on next boot.

## Goals / Non-Goals

**Goals:**
- One deterministic reap decision, applied uniformly to process and non-process workers, that fires exactly at the terminal condition and never leaves an idle worker holding resources.
- Respawn that is event-driven: a dead local worker is recreated only when an event exists for it.
- A liveness model that is correct for both local and remote workers, and that never lets the daemon try to kill/spawn a worker it does not own.

**Non-Goals:**
- Killing or launching remote workers from the daemon (impossible; they are owned by their scheduler). The daemon only manages daemon-side tracking state for them.
- Changing the babysitter process graph, the auto-merge policy, or the MR-watch polling cadence.
- A general idle-timeout policy for freeform (non-MR-owning) workers — that stays as-is.
- Replacing `/proc`; it is retained as a local fast-path.

## Decisions

### D1 — Single reap point at the end of post-merge monitoring
Consolidate all kill logic into one decision reached from the merge/post-merge path. `handlePostMerge` (and the "monitoring disabled" branch of the observed-merge path) computes the terminal condition — MR `merged` AND (main pipeline success OR post-merge monitoring off) — and calls `stopSession` for **local** workers of either kind, then removes the MR-watch entry. The `processName == ""` special-case at `mrs.go:134` and the "let the process handle termination" comment are removed; `tryAutoMerge`'s eager `stopSession` (`:655`) is folded into the same terminal path so an auto-merged worker is only reaped once its main pipeline is confirmed (not immediately at merge).

- *Alternative — kill immediately on merge:* simplest, but it kills before the post-merge main pipeline is known; a `main_pipeline_failed` then has no worker and forces a respawn. Rejected: reaping should mean "nothing left to do."
- *Alternative — key reap off a `RUN_COMPLETED` journal event from babysitter:* ties the daemon to the worker self-reporting completion, which is exactly the non-deterministic behavior we are removing. Rejected as the primary trigger (still honored as a short-circuit when present).

### D2 — Reap guard: no pending follow-up
Before reaping, the daemon confirms no actionable follow-up is outstanding: main-pipeline result is terminal (success, or monitoring off) and the worker's durable inbox has no pending messages. This reuses the same inbox-depth check as D4, so reap and respawn share one "is there work?" predicate.

### D3 — Location-agnostic liveness via on-demand NATS probe, `/proc` as fast-path
Liveness is a request/reply probe on a real-time core-NATS subject scoped to the run (same channel class as BTW/interrupt). Only a live worker process replies; a stale KV entry cannot. The daemon resolves liveness as: if classification is `local` and `/proc` shows the process → alive (fast-path, no NATS round-trip); otherwise send the probe with a short timeout and treat a reply as alive.

- *Alternative — KV heartbeat with TTL:* workers refresh a timestamp; daemon reads freshness. Works cross-host but adds continuous KV write churn and the same staleness class that made KV unreliable before. Kept as an optional fallback mode in the `worker` spec, not the primary.
- *Alternative — JetStream consumer "bound"/`num_waiting` state:* infer liveness from whether the inbox consumer has an active subscriber. Fragile across reconnects and consumer API versions. Rejected as authoritative.

### D4 — Event-driven respawn: spawn-on-the-edge + inbox-gated backstop
Primary: `dispatchToWorkerWithType` (`mrs.go:310`) publishes to the durable inbox as today, and additionally — for a `local` target that is not alive (per D3) — invokes the existing `respawn()` machinery. The worker boots and its `deliver_policy: all` consumer catches up on the just-published message.

Backstop: orphan-recovery's per-run gate changes from "respawn if unfinished + MR-not-done + not-alive" to "respawn if `local` + not-alive + inbox `num_pending`/`num_ack_pending > 0`." This covers events that arrived while the daemon was down and JetStream redelivery of unacked messages. The daemon reads consumer info for `worker-{AGENT_ID}` on `pinard-inboxes`/`pinard-processes` via a new `internal/pnats` helper.

- *Alternative — keep eager respawn but shorten the loop:* still wakes workers with no work. Rejected; wastes exactly the resources this change targets.

### D5 — Classification recorded at spawn, inferred conservatively when absent
`cmd_spawn.go` writes `location: "local"` to the KV entry for vignoble-dir spawns; standalone (`--vignoble-name`) launches record `location: "remote"`. When the field is absent (pre-migration entries), the daemon infers `local` only if `/proc` shows the run on this host or the KV `cwd`/`vignoble` resolves under this daemon's vignoble path; otherwise it treats the worker as `remote` (conservative — never kill/spawn something we might not own). Remote workers are excluded from D1/D4; the daemon only reaps their stale daemon-side tracking state (KV/MR-watch), never a process.

### D6 — Preserve crash-loop protection
The `maxOrphanRetries` cap stays on the respawn path so a worker that dies immediately after spawn-on-the-edge cannot loop forever; exhaustion still notifies the conductor (`orphan_exhausted`) and marks the run complete.

## Risks / Trade-offs

- **Reaping a worker that still has legitimate work** → Reap is gated on MR `merged` + terminal main-pipeline + empty inbox (D1/D2); workers that don't own an MR are untouched (non-goal).
- **Probe latency on each orphan-recovery tick (N runs × round-trip)** → `/proc` fast-path avoids the probe for confirmed-local-alive runs; probes are issued only for ambiguous/remote runs, in parallel, with a short timeout.
- **`num_pending` semantics / API availability in the Go client** → Encapsulated behind one `pnats` helper; if consumer info is unavailable the backstop falls back to conservative (do not respawn) so we never regress into eager respawn.
- **Pre-migration entries misclassified** → D5 inference keeps existing local workers manageable via `/proc`/cwd; only genuinely-unattributable workers default to remote (safe: daemon does nothing rather than the wrong thing).
- **Double-spawn race (edge + backstop, or two dispatches)** → `respawn()` already uses `aoc spawn --force` after confirming not-alive and reaps stale registry first; the spawn duplicate-guard plus a single not-alive check per dispatch keep this idempotent.
- **Remote stale state accumulates** → classification-aware cleanup reaps remote KV/MR-watch entries when their MR lands, without touching a process.

## Migration Plan

1. Ship D1/D2 (deterministic reap) first — immediately stops process workers from lingering; no dependency on liveness changes (local reap can use existing `/proc` + MR state).
2. Add D5 classification (`cmd_spawn.go` + worker KV payload) and D3 liveness probe (worker responder + daemon probe with `/proc` fast-path).
3. Switch respawn to D4 (edge dispatch + inbox-gated orphan-recovery), gated behind classification so remote runs are excluded.
4. Rollback: each phase is independent. Reverting D4 restores eager respawn; reverting D1 restores the split kill logic. No persisted schema migration beyond the additive `location` KV field (ignored by old code).

## Open Questions

- Probe vs heartbeat as the shipped default for D3 — probe is the plan; confirm no environment blocks core-NATS request/reply for remote workers behind restrictive egress.
- Should spawn-on-the-edge (D4 primary) share the `maxOrphanRetries` counter with the backstop, or use a separate short cap for interactive dispatch latency?
- Exact freshness window if the optional heartbeat fallback is ever enabled.
