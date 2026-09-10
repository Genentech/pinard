## Purpose

The conductor extension: how it delivers agent/issue events to the conductor LLM.
## Requirements
### Requirement: Event Delivery

The conductor SHALL deliver `issues_new` events as actionable prompts when the issue was not auto-spawned. Delivery SHALL target the parcelle resolved for the issue (see event-flow "Parcelle Resolution for Events"); issues that resolve to no parcelle SHALL be delivered to the dashboard general lane.

#### Scenario: issues_new delivery for assigned issues
- **WHEN** `handleAgentEvent` receives type `issues_new` with `data.auto_spawn === false`
- **THEN** the event is delivered via `sendUserMessage` with `deliverAs: "steer"`
- **AND** it is delivered to the resolved parcelle's maître, or the general lane when no parcelle resolves
- **AND** the message includes: project, issue IID, title, description (truncated to 500 chars), URL, and labels
- **AND** the message ends with: "Investigate this issue and ask the user whether to spawn a worker."

#### Scenario: issues_new delivery for auto-spawned issues
- **WHEN** `handleAgentEvent` receives type `issues_new` with `data.auto_spawn === true`
- **THEN** the event is delivered via `sendUserMessage` with `deliverAs: "followUp"`
- **AND** it is delivered to the resolved parcelle's maître, or the general lane when no parcelle resolves
- **AND** the message uses the standard one-liner format

### Requirement: Conductor Oversees Per-Parcelle Maîtres

The conductor SHALL operate as a per-vignoble dashboard that orchestrates per-parcelle maîtres rather than injecting all parcelles' events into a single context.

#### Scenario: Parcelle-scoped events go to maîtres
- **WHEN** an event resolves to a parcelle
- **THEN** it SHALL be delivered to that parcelle's maître (not to the dashboard context)

#### Scenario: Dashboard spawns or resumes a maître
- **WHEN** a parcelle needs a maître (an event resolves to it, or the human attaches) and none is live
- **THEN** the conductor SHALL spawn or resume that parcelle's maître keyed by parcelle id

### Requirement: Steer an Maître

The conductor/human SHALL be able to send a message into a specific parcelle's maître session.

#### Scenario: Steer delivers to the target maître only
- **WHEN** the human steers parcelle A's maître with a message
- **THEN** the message SHALL be delivered to parcelle A's maître context
- **AND** it SHALL NOT be delivered to any other parcelle's maître or the dashboard general lane

