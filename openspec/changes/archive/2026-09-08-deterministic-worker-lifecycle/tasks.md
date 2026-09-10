## 1. Deterministic reap (D1/D2)

- [x] 1.1 Add a single `reapWorker(sessionName)` helper in `internal/watcher/mrs.go` that kills the tmux session + deletes the `pinard-agents` KV entry + removes the MR-watch entry, and is a no-op for non-`local` workers.
- [ ] 1.2 Implement the terminal-condition predicate: MR `merged` AND (post-merge main pipeline success OR `shouldMonitorPostMerge == false`) AND no pending inbox work (D2 predicate, shared with 3.3).
- [x] 1.3 Move the reap into `handlePostMerge` (fire when the terminal condition is first met) and the monitoring-disabled branch of the observed-merge path; delete the `processName == ""` special-case at `mrs.go:131-135`.
- [x] 1.4 Fold `tryAutoMerge`'s eager `stopSession` (`mrs.go:655`) into the terminal path so an auto-merged worker is reaped only after its main pipeline is confirmed.
- [ ] 1.5 Ensure `orphan_recovery.markRunCompleted` (and terminal reap) do not leave a live tmux session/KV entry for a reaped local run.
- [ ] 1.6 Guard: if actionable follow-up was dispatched and is unresolved (`pipeline_failed`/`review_comment`/`main_pipeline_failed`), skip reap and re-evaluate later.

## 2. Local/remote classification (D5)

- [ ] 2.1 Write `location: "local"` into the KV agent entry from `cmd/aoc/cmd_spawn.go` for vignoble-dir spawns; write `location: "remote"` on the standalone `--vignoble-name` launch path.
- [ ] 2.2 Include `location` in the worker's `session_start` KV payload (`pi-extension/worker/index.ts`), defaulting to `"remote"` when launched standalone.
- [ ] 2.3 Add a daemon-side `classify(runID/agent)` that returns local/remote, inferring `local` from `/proc` presence or KV `cwd`/`vignoble` under this daemon's path when the field is absent, else `remote`.
- [ ] 2.4 Gate all reap/respawn code paths on `classify(...) == local`; for remote entries whose MR has landed, reap only the daemon-side tracking state (KV/MR-watch), never a process.

## 3. Location-agnostic liveness (D3)

- [ ] 3.1 Define a run-scoped real-time (core NATS) liveness subject + request/reply helper in `internal/pnats/` (subject builder in `subjects.go`).
- [ ] 3.2 Add a liveness responder in `pi-extension/worker/index.ts` that replies to the probe while the worker is running (and stops replying on exit).
- [ ] 3.3 Add a daemon liveness resolver: `local` + `/proc` hit ⇒ alive (fast-path); otherwise send the probe with a short timeout, parallelized across runs; treat a reply as alive.
- [ ] 3.4 Replace direct `liveness.LiveRunIDs` gating in `orphan_recovery.go` with the resolver, keeping `/proc` as the local fast-path.

## 4. Event-driven respawn (D4/D6)

- [ ] 4.1 Add an `internal/pnats` helper returning a durable consumer's pending counts (`num_pending`/`num_ack_pending`) for `worker-{AGENT_ID}` on `pinard-inboxes`/`pinard-processes`; conservative fallback (report 0 / "unknown→don't respawn") if consumer info is unavailable.
- [ ] 4.2 Spawn-on-the-edge: in `dispatchToWorkerWithType` (`mrs.go:310`), after publishing to the inbox, if the target is `local` and not alive, invoke `respawn()`.
- [ ] 4.3 Replace the orphan-recovery respawn gate (`orphan_recovery.go:130-133`) with: `local` + not-alive + inbox pending > 0 ⇒ respawn; else leave dead and reap stale registry state.
- [ ] 4.4 Ensure remote runs are never respawned and are not marked failed solely for being unreachable.
- [ ] 4.5 Keep `maxOrphanRetries` crash-loop protection on the respawn path (both edge and backstop).

## 5. Specs, tests, docs

- [ ] 5.1 Go unit tests: terminal-condition predicate (process + non-process, monitoring on/off, pending-follow-up guard) in `internal/watcher`.
- [ ] 5.2 Go unit tests: classification inference (explicit local/remote, absent→inferred), and respawn gate (pending>0 respawns, pending==0 does not, remote never).
- [ ] 5.3 Liveness resolver tests: `/proc` fast-path, probe reply = alive, probe timeout = dead, remote decided by probe only.
- [ ] 5.4 Contract/integration test: dead local worker with a pending inbox message is respawned on dispatch and consumes the message; worker with empty inbox is not respawned.
- [ ] 5.5 Run `go build ./...`, `go vet`, `go test ./internal/... ./cmd/aoc/`, and `cd pi-extension && npm run typecheck`.
- [ ] 5.6 Update `CLAUDE.md` (Worker lifecycle steps 5–6 + orphan-recovery description) to reflect deterministic reap, event-driven respawn, and remote-worker scoping.
