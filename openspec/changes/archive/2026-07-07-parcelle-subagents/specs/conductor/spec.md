## ADDED Requirements

### Requirement: Conductor Oversees Per-Parcelle Overseers

The conductor SHALL operate as a per-vignoble dashboard that orchestrates per-parcelle overseers rather than injecting all parcelles' events into a single context.

#### Scenario: Parcelle-scoped events go to overseers
- **WHEN** an event resolves to a parcelle
- **THEN** it SHALL be delivered to that parcelle's overseer (not to the dashboard context)

#### Scenario: Dashboard spawns or resumes an overseer
- **WHEN** a parcelle needs an overseer (an event resolves to it, or the human attaches) and none is live
- **THEN** the conductor SHALL spawn or resume that parcelle's overseer keyed by parcelle id

### Requirement: Steer an Overseer

The conductor/human SHALL be able to send a message into a specific parcelle's overseer session.

#### Scenario: Steer delivers to the target overseer only
- **WHEN** the human steers parcelle A's overseer with a message
- **THEN** the message SHALL be delivered to parcelle A's overseer context
- **AND** it SHALL NOT be delivered to any other parcelle's overseer or the dashboard general lane

## MODIFIED Requirements

### Requirement: Event Delivery

The conductor SHALL deliver `issues_new` events as actionable prompts when the issue was not auto-spawned. Delivery SHALL target the parcelle resolved for the issue (see event-flow "Parcelle Resolution for Events"); issues that resolve to no parcelle SHALL be delivered to the dashboard general lane.

#### Scenario: issues_new delivery for assigned issues
- **WHEN** `handleAgentEvent` receives type `issues_new` with `data.auto_spawn === false`
- **THEN** the event is delivered via `sendUserMessage` with `deliverAs: "steer"`
- **AND** it is delivered to the resolved parcelle's overseer, or the general lane when no parcelle resolves
- **AND** the message includes: project, issue IID, title, description (truncated to 500 chars), URL, and labels
- **AND** the message ends with: "Investigate this issue and ask the user whether to spawn a worker."

#### Scenario: issues_new delivery for auto-spawned issues
- **WHEN** `handleAgentEvent` receives type `issues_new` with `data.auto_spawn === true`
- **THEN** the event is delivered via `sendUserMessage` with `deliverAs: "followUp"`
- **AND** it is delivered to the resolved parcelle's overseer, or the general lane when no parcelle resolves
- **AND** the message uses the standard one-liner format
