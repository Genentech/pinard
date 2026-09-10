## 1. Decisions (resolved — see design.md)

- [x] 1.1 Build approach (D6): overseer = native interactive-session spawn for v1; `pi-subagents` deferred to an optional delegation layer inside overseers.
- [x] 1.2 Event scoping (D3): Option A — literal `parcelles` segment (`pinard.<vignoble>.parcelles.<parcelle>.agents.<sid>.…`).
- [x] 1.3 Overseer liveness owner: the daemon (extend orphan recovery). Idle-exit deferred (keep active overseers warm in v1).

## 2. Worker session naming (aoc)

- [x] 2.1 Add a tmux-safe sanitizer (forbid `.` and `:`; deterministic) in `internal/session/` and apply it to any name/parcelle used as a tmux target. (`internal/session/name.go` `SanitizeName`; applied at the tmux choke point in `tmux.go` and at name construction in `cmd_spawn.go`.)
- [x] 2.2 Change auto-generated worker name construction in `cmd/aoc/cmd_spawn.go` to `<parcelle>--<project>-<id><rand>` (issue IID or time token as `<id>`), dropping the redundant `<vignoble>` token and keeping the existing collision suffix. (`workerSessionName` helper; parcelle hoisted + reused for the run dir.)
- [x] 2.3 Verify downstream (`StopWorker`, `GetWorkerCwd`, orphan recovery, KV) still keys off name+socket; adjust only where the old name format was assumed. (No old-format parsing exists; scheduler passes explicit `--name`, now sanitized by spawn. tmux methods sanitize idempotently.)
- [x] 2.4 Add/extend Go tests for the new naming + sanitizer (collision-proofing, forbidden chars, parcelle prefix filterability). (`internal/session/name_test.go`, `cmd/aoc/cmd_spawn_test.go`.)

## 3. Event routing & parcelle resolution

- [x] 3.1 Implement 3-step parcelle resolution (explicit `parcelle:` label / KV field → default KindRepo bucket → general lane) as a shared helper. (Go: `internal/watcher/parcelle_resolve.go` `ResolveIssueParcelle`; TS: `lib/logic.ts` `resolveEventParcelle` + `GENERAL_LANE`; both unit-tested.)
- [x] 3.2 Extend issue→parcelle resolution in `internal/watcher/` to feed step 1/2 and expose it to the conductor. (`autoSpawnForIssue` now uses `ResolveIssueParcelle`, always passing an explicit `--parcelle`.)
- [x] 3.3 Restructure agent/worker subjects to the `pinard.<vignoble>.parcelles.<parcelle>.agents.<sid>.{events,inbox,btw,interrupt}` form (Option A): centralized builders in `internal/pnats/subjects.go` + token-based parse; stream wildcards updated (`pinard.*.parcelles.*.agents.*.{events.>,inbox,process.>}`); Go publishers (mrs.go w/ `getWorkerParcelle`, orphan_recovery.go, panel_events.go); worker extension (`agentBase`, always-parcelle default→project); conductor overseer/dashboard consumer filter + publish sites (`resolveParcelle`); `cmd_spawn`/launcher always pass parcelle. lib/classify + Go/TS tests + harness migrated. Go build+tests, TS typecheck+unit all green.
- [x] 3.4 Route unresolved / vignoble-level events (daemon health, scheduler meta, untriaged issues) to the dashboard general lane. (Resolver returns `GENERAL_LANE` fallback; vignoble-level subjects stay dashboard-consumed. Steer-to-overseer wiring lands with the dashboard in Phase D.)

## 4. Per-parcelle overseer lifecycle

- [x] 4.1 Add overseer spawn/resume via Pinard's existing interactive-session spawn path: launcher `--overseer <parcelle>` mode runs the pinard extension with `--session parcelles/<name>/session.jsonl` (resume-if-exists), conductor-grade model, as an interactive Pi TUI in a tmux window. `aoc overseer spawn` creates the window.
- [x] 4.2 Enforce single-overseer-per-parcelle (reuse/attach if live). (`session.EnsureWindow` is a no-op when the parcelle's window already exists.)
- [x] 4.3 Implement resume-on-demand (on event or attach). Keep active overseers warm; idle-exit is deferred (design), so no idle-kill in v1. (`aoc overseer attach` spawns-if-missing then selects; `--session` resumes state.)
- [x] 4.4 Extend the daemon's orphan recovery to overseers. (`OrphanRecovery.recoverOverseers` runs each tick: for every parcelle with a live worker in KV, ensures an overseer window exists in the running `conductor` session; no-op when the dashboard isn't running.)
- [x] 4.5 Implement steer-into-overseer. (An overseer is an interactive Pi session in its tmux window — "steer" = the human attaches (`aoc overseer attach` / window switch) and types natively; no separate NATS channel needed. Worker-directed steer via `send_message` is parcelle-scoped in Phase A.)

## 5. Overseer extension mode (pi-extension)

- [x] 5.1 Add a parcelle-scoped mode to `pi-extension/pinard/` (env/flag `PINARD_PARCELLE=<name>`): `IS_OVERSEER` gates a per-parcelle durable consumer (`AGENT_EVENTS_FILTER` → `parcelles.<PARCELLE>.agents.*.events.>`, consumer `pinard-overseer-<v>-<parcelle>`).
- [x] 5.2 Reuse existing event classification/delivery (`sendUserMessage`, ACK handling) within the parcelle scope. (Overseer runs the same `index.ts` handlers; only the consumer filter differs.)
- [x] 5.3 Ensure autonomous handling when unattended and interactive steering when attended. (Autonomous via `sendUserMessage`; interactive via native TUI in the attached window.)

## 6. Dashboard / control room

- [x] 6.1 Add a dashboard mode to the conductor: subscribe to the lightweight overview only (counts/alerts/notifications), NOT the per-parcelle firehose. (`IS_OVERSEER` gates the agent-events consumer; dashboard keeps notifications/issues/schedules consumers.)
- [x] 6.2 Rail overview: `list_parcelles` tool groups workers by parcelle from the authoritative KV `parcelle` field and reports overseer-window running state. (The Go `ParcellesPanel` remains a separate monitoring TUI; the LLM dashboard gets its overview via this tool — a TS extension can't embed the Go TUI. Pending-gate counts deferred with idle-exit.)
- [x] 6.3 General lane persistent session: dashboard runs with `--session parcelles/_general/session.jsonl` (resume-if-exists). Promotion = dashboard spawns a new overseer via `attach_parcelle`/`aoc overseer`.
- [x] 6.4 Attach/switch = tmux window switch: `attach_parcelle` tool + `/parcelle <name>` command → `aoc overseer attach` (spawns-if-missing, then `select-window`).

## 7. tmux topology & launcher

- [x] 7.1 Control-room window layout: `conductor` session's window 0 is `dashboard`; overseers are sibling windows (`aoc overseer spawn`, `session.EnsureWindow`); workers remain flat sessions on the same socket.
- [x] 7.2 Update `bin/pinard` launcher to start the dashboard vs. an overseer (parcelle-scoped) mode. (`--overseer <parcelle>` mode; shared `_build_context_args`/`_PINARD_TOOLS`.)
- [x] 7.3 Confirm the 2-level view: `tmux -L pinard-<v> ls` → conductor + workers; `list-windows -t conductor` → dashboard + overseers (`aoc overseer list`).

## 8. Specs, tests, docs

- [x] 8.1 Add/adjust Go + TS tests: subject builders/parser round-trip (`pnats/subjects_test.go`), naming + tmux-safe sanitizer (`session/name_test.go`, `cmd_spawn_test.go`), parcelle resolution (`watcher/parcelle_resolve_test.go`, `resolve-parcelle.test.ts`), migrated dispatch/inbox/classify/event-panel tests. (Per-parcelle isolation is enforced by subject filters — covered by the subject/parse tests + updated harness; a live-NATS isolation contract test is left for the integration run.)
- [x] 8.2 Ran `go test ./internal/... ./cmd/aoc/` (all pass), `cd tests && npm run test:unit` (136 pass), `cd pi-extension && npm run typecheck` (clean). Integration/contract (`test:integration`/`test:contract`) need a live NATS server — harness migrated but not run here.
- [x] 8.3 Updated `CLAUDE.md` (component table → dashboard/overseer, three-tier orchestration, parcelle-scoped NATS subjects, worker naming + tmux topology, file layout incl. `cmd_overseer.go`/`session/`/`pnats/subjects.go`/`lib/`).
- [x] 8.4 Validated the change (`openspec validate --changes parcelle-subagents` → valid). Archive (`/opsx:archive`) deferred until reviewed/merged + integration/contract run against live NATS.

## 9. Deferred (out of scope for v1)

- [ ] 9.1 (Future) Adopt `pi-subagents` as a delegation layer inside overseers/dashboard for bounded fan-out (`scout`/`reviewer`/`researcher`), including `PI_SUBAGENT_PI_BINARY` → vendored runtime wiring and dist bundling. Not required for v1.
- [ ] 9.2 (Future) Idle-exit policy for overseers (timeout + pending-gate/alert exemption), only if many-active-parcelle load warrants it.
