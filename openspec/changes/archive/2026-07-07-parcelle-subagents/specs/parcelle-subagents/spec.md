## ADDED Requirements

### Requirement: Persistent Per-Parcelle Overseer Session

The system SHALL run at most one overseer subagent per parcelle per vignoble, using a conductor-grade model, with persistent session state keyed by the parcelle id.

#### Scenario: Session file location and key
- **WHEN** an overseer is spawned or resumed for parcelle `<name>`
- **THEN** its session state SHALL be stored at `parcelles/<name>/session.jsonl`
- **AND** the parcelle id SHALL be the session key (no other keyspace)

#### Scenario: Resume-if-exists
- **WHEN** an overseer for parcelle `<name>` is (re)started AND `parcelles/<name>/session.jsonl` exists
- **THEN** the overseer SHALL resume from that session file rather than starting fresh
- **AND** WHEN the session file does not exist THEN it SHALL start a fresh session and create the file

#### Scenario: Single overseer per parcelle
- **WHEN** a request is made to spawn an overseer for a parcelle that already has a live overseer process
- **THEN** the system SHALL NOT start a second overseer for that parcelle
- **AND** it SHALL reuse (or attach to) the existing one

#### Scenario: Conductor-grade model
- **WHEN** an overseer is spawned
- **THEN** it SHALL use a conductor-grade model (it SHALL NOT be downgraded to the worker model by default)

### Requirement: Idle-Exit and Resume-on-Demand

An overseer SHALL persist session state, not necessarily a running process. An idle overseer MAY exit and SHALL be resumable on demand.

#### Scenario: Idle overseer exits
- **WHEN** an overseer has been idle beyond the configured idle timeout AND has no pending gates or unacknowledged alerts
- **THEN** its process MAY exit
- **AND** its `parcelles/<name>/session.jsonl` SHALL be preserved

#### Scenario: Event resumes an exited overseer
- **WHEN** an event resolves to a parcelle whose overseer process is not running
- **THEN** the system SHALL resume that overseer from its session file before delivering the event

#### Scenario: Attach resumes an exited overseer
- **WHEN** the human switches to a parcelle window whose overseer process is not running
- **THEN** the overseer SHALL be resumed from its session file

### Requirement: Autonomous and Attachable Overseer

An overseer SHALL act autonomously on its parcelle's events when unattended, and SHALL accept interactive steering when attended.

#### Scenario: Autonomous handling when unattended
- **WHEN** an event is delivered to an overseer and no human is attached to its window
- **THEN** the overseer SHALL process the event and may act (spawn workers, comment, track MRs) without waiting for a human

#### Scenario: Interactive steering when attended
- **WHEN** the human attaches to a parcelle's overseer and sends a message
- **THEN** the message SHALL be delivered to that overseer's session and processed in its context

### Requirement: Per-Parcelle Event Isolation

An overseer SHALL receive only events that resolve to its own parcelle.

#### Scenario: Overseer receives only its parcelle's events
- **WHEN** events for parcelle A and parcelle B are published
- **THEN** parcelle A's overseer SHALL receive only parcelle A's events
- **AND** parcelle B's events SHALL NOT enter parcelle A's context

### Requirement: Overseer Orphan Recovery

The system SHALL detect and recover overseers whose process died while their parcelle still has active or pending work.

#### Scenario: Dead overseer with pending work is recovered
- **WHEN** a parcelle has active workers or pending gates AND its overseer process is not alive
- **THEN** the system SHALL resume the overseer from its session file
- **AND** recovery SHALL use authoritative process liveness (not stale registry state)
