## ADDED Requirements

### Requirement: Deterministic Worker Reap

The daemon SHALL be the single authority that terminates a worker, and it SHALL do so deterministically when the worker's work is terminally complete. There is exactly one reap decision point; workers (including process/SWE workers) MUST NOT be relied upon to self-terminate.

The terminal condition for a worker owning an MR is: the MR is `merged` **AND** either the post-merge main pipeline for the merge commit succeeded, **or** post-merge monitoring is disabled for the vigne. Reap applies uniformly to process and non-process workers, and only to `local` workers (per `worker-liveness`).

#### Scenario: Reap after clean landing
- **WHEN** a local worker's MR is `merged` and the post-merge main pipeline for the merge commit reports success
- **THEN** the daemon reaps the worker (kills the tmux session and deletes its `pinard-agents` KV entry) and removes the MR-watch entry

#### Scenario: Reap when post-merge monitoring is disabled
- **WHEN** a local worker's MR is `merged` and the vigne has post-merge monitoring disabled
- **THEN** the daemon reaps the worker without waiting for a main-pipeline result

#### Scenario: Process workers are reaped, not self-terminated
- **WHEN** the terminal condition is met for a process (SWE) worker
- **THEN** the daemon reaps it the same as a non-process worker
- **AND** the daemon does not defer termination to the worker process

#### Scenario: Do not reap while follow-up work is pending
- **WHEN** the daemon has dispatched actionable follow-up (e.g. `main_pipeline_failed`, `review_comment`, `pipeline_failed`) that has not been resolved
- **THEN** the worker is not reaped
- **AND** reap is reconsidered only once the terminal condition is met with no pending follow-up

#### Scenario: Reap does not resurrect
- **WHEN** a worker has been reaped at the terminal condition
- **THEN** orphan-recovery does not respawn it (its run is complete and no event is pending)

### Requirement: On-Demand Worker Respawn

A dead `local` worker SHALL be respawned only when there is an event for it — never eagerly on a timer. Respawn relies on the durable JetStream inbox (`deliver_policy: all`, consumer `worker-{AGENT_ID}`), so a message published to a dead worker's inbox is caught up when the worker boots.

#### Scenario: Spawn-on-the-edge at dispatch
- **WHEN** the daemon dispatches an actionable event to a `local` worker whose process is not alive (per `worker-liveness`)
- **THEN** the daemon spawns the worker (resume by run ID) and publishes the event to its durable inbox
- **AND** the worker consumes the pending event once it reaches its event-wait step

#### Scenario: Orphan-recovery respawns only when the inbox has pending work
- **WHEN** orphan-recovery examines a `local` run whose worker is not alive
- **THEN** it respawns the worker only if that worker's durable inbox consumer reports pending messages (`num_pending`/`num_ack_pending > 0`)
- **AND** if the inbox has no pending messages it leaves the worker dead and reaps any stale registry/tracking state

#### Scenario: No idle warm workers
- **WHEN** a run is unfinished but has no pending inbox event and its MR is not awaiting the worker
- **THEN** the daemon does not keep or respawn a worker for it
- **AND** the worker is (re)created only when a future event arrives

#### Scenario: Remote worker not respawned
- **WHEN** an event is dispatched for a `remote` worker that is down
- **THEN** the daemon does not spawn it
- **AND** the event is retained in the durable inbox for delivery when the remote worker returns
