## MODIFIED Requirements

### Requirement: State Publishing

Workers SHALL publish their state to the `pinard-agents` KV bucket so the daemon can track them. The published state MUST include the worker's local/remote classification so the daemon knows whether it may reap or respawn the worker.

#### Scenario: Session start
- **WHEN** the worker extension receives `session_start` event
- **THEN** it publishes to KV key `{session}` with state `"running"` and tempo `"active"`
- **AND** the payload includes: `name`, `session_id`, `project` (from `WORKER_PROJECT` env), `mr: null`, `state: "running"`, `tempo: "active"`, `cwd`, `vignoble`
- **AND** the payload includes a `location` classification of `"local"` or `"remote"` (defaulting to `"remote"` when the worker was launched standalone via `--vignoble-name`)

#### Scenario: Turn start
- **WHEN** the LLM begins a turn (`turn_start` event)
- **THEN** KV is updated with `tempo: "active"`

#### Scenario: Turn end
- **WHEN** the LLM finishes a turn (`turn_end` event)
- **THEN** KV is updated with `tempo: "blocked"` (waiting for tool results or user input)

#### Scenario: Session end
- **WHEN** the worker session ends (`session_end` event)
- **THEN** the KV entry for this session is deleted
- **AND** a `session_ended` event is published to `pinard.{vignoble}.agents.{session}.events.session_ended`
- **AND** the JetStream consumer `worker-{session}` is deleted (cleanup)

## ADDED Requirements

### Requirement: Liveness Responder

A worker SHALL make its liveness observable over NATS so the daemon can determine liveness independent of host (local or remote). The worker MUST answer a liveness probe on a real-time (core NATS) subject scoped to its run, and/or publish a periodic heartbeat.

#### Scenario: Answer liveness probe
- **WHEN** the daemon sends a liveness probe (request) on the worker's run-scoped liveness subject
- **THEN** a running worker replies before the probe timeout
- **AND** a worker that has exited never replies

#### Scenario: Heartbeat freshness (if heartbeat mode is used)
- **WHEN** a worker is running
- **THEN** it refreshes a liveness marker (heartbeat timestamp) within the daemon's freshness window
- **AND** the daemon treats a stale marker (older than the window) as not-alive

#### Scenario: No response after exit
- **WHEN** a worker process has terminated
- **THEN** no reply to the probe is produced and no heartbeat is refreshed
- **AND** the daemon may conclude the worker is dead
