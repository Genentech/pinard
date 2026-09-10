## ADDED Requirements

### Requirement: Group ID Assignment

Workers SHALL be assigned a `group_id` at spawn time that scopes their knowledge queries.

#### Scenario: Explicit group from spawn
- **WHEN** a worker is spawned via `aoc spawn --group example-build`
- **THEN** `WORKER_GROUP_ID` env var is set to `example-build`

#### Scenario: Group from schedule
- **WHEN** a worker is spawned from a schedule entry that has a `group_id` field
- **THEN** `WORKER_GROUP_ID` is set to that schedule's `group_id`

#### Scenario: Default group from project name
- **WHEN** no `--group` is specified and no schedule `group_id` exists
- **THEN** `WORKER_GROUP_ID` defaults to the project name

---

### Requirement: Recipe Query at Boot

Workers SHALL query accumulated recipes for their pipeline group on startup.

#### Scenario: NATS request-reply query
- **WHEN** the worker connects to NATS on `session_start`
- **THEN** it publishes a request to `pinard.{vignoble}.memory.query` with payload `{"group_id": "<id>", "max_facts": 50}`
- **AND** the episode ingester (or dedicated query service) responds with serialized facts
- **AND** the response is injected into the worker's context via `sendUserMessage` with `deliverAs: "steer"`
- **AND** if no response is received within 5 seconds, the worker proceeds without recipes

#### Scenario: Git-based fallback
- **WHEN** the NATS query times out or the memory service is unavailable
- **THEN** the worker reads `recipes/{group_id}/current.yaml` from the vignoble git repo
- **AND** if the file exists, its contents are parsed and injected into the worker context
- **AND** if the file does not exist, the worker proceeds without recipes

---

### Requirement: Injection Format

Injected recipes SHALL be structured as human-readable operational knowledge.

#### Scenario: Context injection content
- **WHEN** recipes are injected into the worker
- **THEN** the injection message is prefixed with `[pipeline-knowledge]`
- **AND** includes sections for: Known Issues (LogPattern → Diagnosis → Action chains), Pipeline Dependencies, Environment Prerequisites, and Decision Points
- **AND** uses `deliverAs: "steer"` so it does not trigger a new LLM turn

#### Scenario: Empty recipe set
- **WHEN** no facts exist for the worker's `group_id`
- **THEN** no injection message is sent
- **AND** the worker proceeds normally without any recipe context
