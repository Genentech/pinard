## ADDED Requirements

### Requirement: Teaching Mode Command

The conductor SHALL register a `/teaching` command for toggling knowledge extraction mode.

#### Scenario: Command registration
- **WHEN** the conductor extension initializes
- **THEN** it registers `/teaching` as a conductor command
- **AND** the command accepts optional arguments: `on`, `off`, `stop`, `--from <duration>`, `--all`

#### Scenario: Teaching state published to NATS
- **WHEN** teaching mode is activated
- **THEN** the conductor publishes to `pinard.{vignoble}.memory.teaching.{conductor_session}` with `{"active": true}`
- **AND** the connected worker's session is notified so its episodes are tagged `mode: "teaching"`

#### Scenario: Teaching state in conductor memory
- **WHEN** teaching mode is active
- **THEN** `teachingMode` flag is available to the conductor's event processing logic
- **AND** user messages forwarded to workers include the teaching mode signal

---

### Requirement: Conductor Episode Publishing

The conductor SHALL publish user messages as episodes when in teaching mode.

#### Scenario: User teaching turn published
- **WHEN** teaching mode is active and the human sends a message
- **THEN** the conductor publishes to `pinard.{vignoble}.memory.episodes`
- **AND** `source` is `"conductor"`, `mode` is `"teaching"`, `episode.role` is `"user"`

#### Scenario: Normal mode does not publish user turns
- **WHEN** teaching mode is not active
- **THEN** the conductor does NOT publish user messages as episodes
- **AND** only worker-side episode publishing operates
