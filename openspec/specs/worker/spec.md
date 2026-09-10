## MODIFIED Requirements

### Requirement: State Publishing

Workers SHALL publish their state to the `pinard-agents` KV bucket so the daemon can track them.

#### Scenario: Session start
- **WHEN** the worker extension receives `session_start` event
- **THEN** it publishes to KV key `{session}` with state `"running"` and tempo `"active"`
- **AND** the payload includes: `name`, `session_id`, `project` (from `WORKER_PROJECT` env), `mr: null`, `state: "running"`, `tempo: "active"`, `cwd`, `vignoble`

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

## REMOVED Requirements

### Requirement: tmux session awareness
**Reason**: Workers run inside tmux sessions (one session per worker on the vignoble's tmux socket). The worker extension itself is unaware of the session container — it communicates exclusively via NATS. No code changes needed in the worker extension.
**Migration**: None. The worker extension has no tmux-specific code. Session containers are created and destroyed by `cmd_spawn.go` and `watcher/mrs.go`.
