# conductor-control-room Specification

## Purpose
TBD - created by archiving change parcelle-subagents. Update Purpose after archive.
## Requirements
### Requirement: Dashboard Control Room

The top-level conductor SHALL run as a dashboard/control-room per vignoble that presents a cross-parcelle overview and does NOT consume the per-parcelle event firehose.

#### Scenario: Overview rail
- **WHEN** the dashboard is running
- **THEN** it SHALL display a rail of parcelles with per-parcelle status (at minimum: worker count, pending gates, unread/alert count, maître state)
- **AND** per-parcelle worker counts SHALL be derived from the authoritative KV `parcelle` field, not from tmux session-name parsing

#### Scenario: Dashboard does not receive the firehose
- **WHEN** parcelle-scoped agent events are published
- **THEN** the dashboard SHALL NOT inject them into its context
- **AND** it SHALL consume only a lightweight overview (counts/alerts)

### Requirement: General Lane

The dashboard SHALL own a general lane for events that resolve to no parcelle, and SHALL persist its own session.

#### Scenario: Unrouted event lands in the general lane
- **WHEN** an event resolves to no parcelle (e.g. daemon health, scheduler meta, untriaged issue)
- **THEN** it SHALL be delivered to the general lane, not to any parcelle maître

#### Scenario: General lane session is persistent
- **WHEN** the dashboard/general lane starts
- **THEN** its session state SHALL persist at a reserved location (`parcelles/_general/session.jsonl` or vignoble-root)
- **AND** it SHALL resume-if-exists

#### Scenario: Promotion births a parcelle
- **WHEN** work in the general lane is assigned to a new parcelle
- **THEN** the system SHALL create that parcelle and route its subsequent events to a (new) maître for it

### Requirement: Control-Room tmux Topology

Maîtres SHALL be presented as tmux windows within the conductor session on the existing per-vignoble tmux server; workers SHALL remain flat tmux sessions on the same server.

#### Scenario: One window per parcelle plus dashboard
- **WHEN** the control room is running with active parcelles A and B
- **THEN** the conductor session SHALL have a dashboard window AND one maître window per active parcelle
- **AND** no additional tmux server SHALL be created per parcelle

#### Scenario: Switch attaches to the real maître session
- **WHEN** the human selects a parcelle from the dashboard (or switches tmux window)
- **THEN** tmux SHALL switch to that parcelle maître's own full Pi TUI window (no mirroring)
- **AND** exactly one full-screen view SHALL be shown at a time (no split panes)

#### Scenario: Two-level listing
- **WHEN** the operator lists tmux sessions on the vignoble server
- **THEN** they SHALL see the conductor session and the worker sessions
- **AND** listing the conductor session's windows SHALL show the parcelle maîtres

