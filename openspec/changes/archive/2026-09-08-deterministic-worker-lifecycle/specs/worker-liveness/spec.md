## ADDED Requirements

### Requirement: Authoritative Liveness Signal

The daemon SHALL determine whether a worker is alive using a signal that is valid regardless of where the worker runs. The authoritative signal is NATS-based (a request/reply liveness probe or a recent heartbeat). The `/proc` scan (`liveness.LiveRunIDs`) MAY be used only as a local fast-path optimization and MUST NOT be treated as authoritative for workers not known to be local.

#### Scenario: Local worker, alive
- **WHEN** the daemon probes a local worker's run and the worker replies to the NATS liveness probe (or has a heartbeat within the freshness window)
- **THEN** the worker is considered alive
- **AND** the daemon does not reap or respawn it

#### Scenario: Local worker, dead
- **WHEN** a local worker does not reply to the liveness probe within the timeout and has no fresh heartbeat, and `/proc` shows no live process for its run
- **THEN** the worker is considered dead

#### Scenario: Remote worker liveness over NATS only
- **WHEN** the daemon evaluates a remote worker (no local process to scan)
- **THEN** liveness is decided solely by the NATS probe/heartbeat
- **AND** absence of a `/proc` entry MUST NOT be interpreted as the worker being dead

#### Scenario: Probe answered by exactly one live worker
- **WHEN** the daemon sends a liveness probe for a run
- **THEN** only a live worker process for that run replies
- **AND** a stale KV entry alone never produces a "live" verdict

### Requirement: Local vs Remote Worker Classification

Every worker SHALL be classified as `local` (daemon-spawnable on this host) or `remote` (externally scheduled, e.g. HPC/Singularity launched via `--vignoble-name` over NATS). The classification MUST be recorded in the worker's `pinard-agents` KV entry at spawn so the daemon can decide what it may manage.

#### Scenario: Local worker recorded
- **WHEN** a worker is spawned by `aoc spawn` with a resolved vignoble directory on the daemon host
- **THEN** its KV entry records classification `local`

#### Scenario: Remote worker recorded
- **WHEN** a worker is launched standalone with `--vignoble-name` (no vignoble directory on the daemon host)
- **THEN** its KV entry records classification `remote`

#### Scenario: Unknown classification treated as remote
- **WHEN** a worker's KV entry lacks a classification field
- **THEN** the daemon treats it conservatively as `remote` and does not attempt to reap or respawn it

### Requirement: Daemon Management Scoped by Classification

Daemon-driven reaping and respawning SHALL apply only to `local` workers. The daemon MUST NOT kill or spawn `remote` workers; remote workers are (re)launched by their own scheduler and rely on the durable inbox to catch up when they return.

#### Scenario: Remote worker never respawned by orphan-recovery
- **WHEN** orphan-recovery finds a `remote` run whose worker is not answering the liveness probe
- **THEN** it does not respawn it
- **AND** it does not mark the run failed solely for being unreachable (the run may resume later on its own scheduler)

#### Scenario: Remote worker not force-killed
- **WHEN** a `remote` worker's work reaches the terminal condition
- **THEN** the daemon does not attempt to kill the remote process
- **AND** it releases only daemon-side tracking state (e.g. stale KV/MR-watch entries) as appropriate

#### Scenario: Remote worker catches up via durable inbox
- **WHEN** an event is dispatched for a `remote` worker that is currently down
- **THEN** the event is published to the worker's durable inbox and retained
- **AND** it is delivered when the remote worker next connects (deliver_policy `all`)
