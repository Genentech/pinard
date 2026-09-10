## MODIFIED Requirements

### Requirement: Event Delivery

The conductor SHALL deliver `issues_new` events as actionable prompts when the issue was not auto-spawned.

#### Scenario: issues_new delivery for assigned issues
- **WHEN** `handleAgentEvent` receives type `issues_new` with `data.auto_spawn === false`
- **THEN** the event is delivered via `sendUserMessage` with `deliverAs: "steer"`
- **AND** the message includes: project, issue IID, title, description (truncated to 500 chars), URL, and labels
- **AND** the message ends with: "Investigate this issue and ask the user whether to spawn a worker."

#### Scenario: issues_new delivery for auto-spawned issues
- **WHEN** `handleAgentEvent` receives type `issues_new` with `data.auto_spawn === true`
- **THEN** the event is delivered via `sendUserMessage` with `deliverAs: "followUp"`
- **AND** the message uses the standard one-liner format
