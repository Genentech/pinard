## MODIFIED Requirements

### Requirement: State Publishing

Workers SHALL publish their state to the `pinard-agents` KV bucket so the conductor can track them.

#### Scenario: Session start
- **WHEN** the worker extension receives `session_start` event
- **THEN** it publishes to KV key `{session}` with state `"running"` and tempo `"active"`
- **AND** the payload includes: `name`, `session_id`, `project` (from `WORKER_PROJECT` env), `mr: null`, `state: "running"`, `tempo: "active"`, `cwd`, `vignoble`, `group_id` (from `WORKER_GROUP_ID` env)

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

## ADDED Requirements

### Requirement: Episode Publishing

Workers SHALL publish conversation transcripts to the memory stream for knowledge extraction.

#### Scenario: Transcript publish on message_end
- **WHEN** the `message_end` hook fires with an assistant message containing text
- **THEN** the worker publishes an episode payload to `pinard.{vignoble}.memory.episodes`
- **AND** `group_id` is read from `WORKER_GROUP_ID` env var
- **AND** `mode` is `"teaching"` if the worker has received a teaching activation signal, `"normal"` otherwise

#### Scenario: Tool-only responses not published
- **WHEN** the `message_end` hook fires with a response containing only tool calls and no text
- **THEN** no episode is published

---

### Requirement: Boot Recipe Injection

Workers SHALL query and inject accumulated recipes for their pipeline group on startup.

#### Scenario: Recipe query on session_start
- **WHEN** the worker connects to NATS on `session_start`
- **AND** `WORKER_GROUP_ID` env var is set
- **THEN** it publishes a NATS request to `pinard.{vignoble}.memory.query` with `{"group_id": "<id>", "max_facts": 50}`
- **AND** injects the response into context via `sendUserMessage` with `deliverAs: "steer"`
- **AND** if no response within 5 seconds, falls back to reading `recipes/{group_id}/current.yaml`
- **AND** if neither source yields data, proceeds without recipes

#### Scenario: No group_id configured
- **WHEN** `WORKER_GROUP_ID` is not set
- **THEN** no recipe query is performed
- **AND** the worker proceeds normally

---

### Requirement: Per-Turn Chunk Publishing

Workers SHALL publish each conversation turn to the memory stream for near-real-time vector embedding, independently of session-end episode publishing.

#### Scenario: Chunk publish on message_end
- **WHEN** the `message_end` hook fires with an assistant message containing text
- **THEN** the worker publishes to `pinard.{vignoble}.memory.chunks`
- **AND** the payload includes: `session_id`, `group_id`, `turn_index`, `role` ("assistant"), `content` (truncated to 2000 chars), `context` (project, step if known), `timestamp`
- **AND** this is independent of the session-end episode publish to `memory.episodes`

#### Scenario: Tool-only responses not published as chunks
- **WHEN** the `message_end` hook fires with a response containing only tool calls and no text
- **THEN** no chunk is published

#### Scenario: No group_id — chunks still published
- **WHEN** `WORKER_GROUP_ID` is not set
- **THEN** chunks are still published with `group_id: null`
- **AND** they are searchable but not group-scoped

---

### Requirement: Mid-Session Recall

Workers SHALL query the memory service each turn for relevant accumulated knowledge.

#### Scenario: Recall query on message_end
- **WHEN** the `message_end` hook fires with an assistant message
- **THEN** the worker publishes a NATS request to `pinard.{vignoble}.memory.recall`
- **AND** the request includes: `session_id`, `group_id`, last user message (truncated 500 chars), last assistant response (truncated 500 chars), `turn_index`, `max_context_tokens: 400`, `exclude_session: <self>`
- **AND** waits up to 3 seconds for a response

#### Scenario: Non-empty recall response
- **WHEN** the memory service responds with a non-null `context`
- **THEN** the worker injects it via `sendUserMessage` with `deliverAs: "steer"` before the next LLM turn

#### Scenario: Empty or timed-out recall
- **WHEN** the memory service responds with `{"context": null}` or the request times out
- **THEN** the worker proceeds normally with no injection

#### Scenario: Memory service unavailable
- **WHEN** the NATS request fails (no responders)
- **THEN** the worker proceeds without mid-session context
- **AND** no error is surfaced to the LLM
