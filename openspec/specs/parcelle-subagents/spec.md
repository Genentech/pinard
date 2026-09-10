# parcelle-subagents Specification

## Purpose
TBD - created by archiving change parcelle-subagents. Update Purpose after archive.
## Requirements
### Requirement: Persistent Per-Parcelle Maître Session

The system SHALL run at most one maître subagent per parcelle per vignoble, using a conductor-grade model, with persistent session state keyed by the parcelle id.

#### Scenario: Session file location and key
- **WHEN** a maître is spawned or resumed for parcelle `<name>`
- **THEN** its session state SHALL be stored at `parcelles/<name>/session.jsonl`
- **AND** the parcelle id SHALL be the session key (no other keyspace)

#### Scenario: Resume-if-exists
- **WHEN** a maître for parcelle `<name>` is (re)started AND `parcelles/<name>/session.jsonl` exists
- **THEN** the maître SHALL resume from that session file rather than starting fresh
- **AND** WHEN the session file does not exist THEN it SHALL start a fresh session and create the file

#### Scenario: Single maître per parcelle
- **WHEN** a request is made to spawn a maître for a parcelle that already has a live maître process
- **THEN** the system SHALL NOT start a second maître for that parcelle
- **AND** it SHALL reuse (or attach to) the existing one

#### Scenario: Conductor-grade model
- **WHEN** a maître is spawned
- **THEN** it SHALL use a conductor-grade model (it SHALL NOT be downgraded to the worker model by default)

### Requirement: Idle-Exit and Resume-on-Demand

An maître SHALL persist session state, not necessarily a running process. An idle maître MAY exit and SHALL be resumable on demand.

#### Scenario: Idle maître exits
- **WHEN** a maître has been idle beyond the configured idle timeout AND has no pending gates or unacknowledged alerts
- **THEN** its process MAY exit
- **AND** its `parcelles/<name>/session.jsonl` SHALL be preserved

#### Scenario: Event resumes an exited maître
- **WHEN** an event resolves to a parcelle whose maître process is not running
- **THEN** the system SHALL resume that maître from its session file before delivering the event

#### Scenario: Attach resumes an exited maître
- **WHEN** the human switches to a parcelle window whose maître process is not running
- **THEN** the maître SHALL be resumed from its session file

### Requirement: Autonomous and Attachable Maître

An maître SHALL act autonomously on its parcelle's events when unattended, and SHALL accept interactive steering when attended.

#### Scenario: Autonomous handling when unattended
- **WHEN** an event is delivered to a maître and no human is attached to its window
- **THEN** the maître SHALL process the event and may act (spawn workers, comment, track MRs) without waiting for a human

#### Scenario: Interactive steering when attended
- **WHEN** the human attaches to a parcelle's maître and sends a message
- **THEN** the message SHALL be delivered to that maître's session and processed in its context

### Requirement: Per-Parcelle Event Isolation

An maître SHALL receive only events that resolve to its own parcelle.

#### Scenario: Maître receives only its parcelle's events
- **WHEN** events for parcelle A and parcelle B are published
- **THEN** parcelle A's maître SHALL receive only parcelle A's events
- **AND** parcelle B's events SHALL NOT enter parcelle A's context

### Requirement: Maître Orphan Recovery

The system SHALL detect and recover maîtres whose process died while their parcelle still has active or pending work.

#### Scenario: Dead maître with pending work is recovered
- **WHEN** a parcelle has active workers or pending gates AND its maître process is not alive
- **THEN** the system SHALL resume the maître from its session file
- **AND** recovery SHALL use authoritative process liveness (not stale registry state)

