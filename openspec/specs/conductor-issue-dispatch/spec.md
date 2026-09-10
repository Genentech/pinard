## ADDED Requirements

### Requirement: Conductor reacts to assigned issues

The conductor SHALL actively handle `issues_new` events for issues that were not auto-spawned.

#### Scenario: New issue assigned without auto-pinard label
- **WHEN** an `issues_new` event arrives with `auto_spawn: false`
- **THEN** the conductor receives it via `deliverAs: "steer"` (wakes the idle conductor)
- **AND** the message includes the issue title, description, URL, project, and labels
- **AND** the message instructs the conductor to investigate and ask the user whether to spawn

#### Scenario: New issue with auto-pinard label
- **WHEN** an `issues_new` event arrives with `auto_spawn: true`
- **THEN** the conductor receives it via `deliverAs: "followUp"` (passive log entry)
- **AND** the message is the standard one-liner format (no investigation prompt)
