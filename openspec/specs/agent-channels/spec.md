## Purpose

This specification defines the three communication channels between the pinard conductor and workers. Each channel serves a distinct purpose, uses a different delivery mechanism, and has different reply semantics. The design eliminates lost replies and enables parallel side-conversations without interrupting a worker's primary task.

## Components

| Component | Role in this spec |
|-----------|-------------------|
| **Conductor** | Sends messages via all three channels, receives btw replies |
| **Worker** | Receives on all three channels, responds automatically on btw |
| **pi-btw** | Pi package providing parallel sub-sessions with tool access |

## Channels Overview

| Channel | NATS subject | Use case | Delivery | Reply |
|---------|-------------|----------|----------|-------|
| **Main inbox** | `pinard.{v}.agents.{session}.inbox` | Actionable work | `sendUserMessage` — queued until turn ends | None — outcome via events |
| **BTW** | `pinard.{v}.agents.{session}.btw` | Questions, investigations | pi-btw sub-session — immediate, parallel | Automatic → `btw_reply` event |
| **Interrupt** | `pinard.{v}.agents.{session}.interrupt` | Stop current work | Cancels active LLM turn | None |

## Requirements

### Requirement: Main Inbox Channel

The main inbox delivers actionable instructions to the worker's LLM. Messages queue until the current turn ends. This channel is unchanged from the event-flow spec.

#### Scenario: Conductor sends work instruction
- **WHEN** the conductor publishes to `pinard.{v}.agents.{session}.inbox`
- **THEN** the worker extension delivers the message via `sendUserMessage(text, { deliverAs: "followUp" })`
- **AND** the message is processed after the worker's current LLM turn completes
- **AND** no reply is expected — the conductor observes outcomes via events (`pipeline_passed`, `agent_idle`, `mr_merged`, etc.)

#### Scenario: Automatic dispatch uses main inbox
- **WHEN** `dispatchToWorker()` fires for `pipeline_failed`, `review_comment`, `main_pipeline_failed`, or `tag_pipeline_failed`
- **THEN** the dispatch message is published to the main inbox
- **AND** it is NOT sent via the btw channel

#### Scenario: Conductor-only events must not be forwarded
- **WHEN** the conductor LLM receives a conductor-only event (`needs_approval`, `circuit_breaker`, `schedule_*`, `mr_merged`, `mr_closed`, `issues_new`)
- **THEN** it SHALL NOT use `send_message` to forward that event to any worker
- **AND** these events are for conductor awareness only

---

### Requirement: Process-Worker Inbox Message Typing

A **process worker** (one governed by a babysitter process, identified by `PROCESS_NAME`) SHALL distinguish inbox messages by the presence of a `type` field: **typed** messages MAY resolve a babysitter event effect, while **typeless** messages SHALL be delivered to the worker's LLM as conversation and SHALL NOT resolve or advance the babysitter process. A typeless message SHALL NOT be dropped on account of process state.

Process workers subscribe to a process-scoped inbox `pinard.{v}.agents.{agentId}.process.{name}.inbox` instead of the freeform `…inbox`. The two kinds are handled differently while the process is parked in **event-wait** (babysitter blocked on an `event` effect, signalled by a `.babysitter-event-wait.json` file):

- **Typed** messages (`type` present) are daemon events (`pipeline_failed`, `mr_merged`, etc.) — the only messages that may resolve a babysitter event effect.
- **Typeless** messages (no `type`) are conductor/human chat sent via `send_message` (without `ask`) — conversation for the worker's LLM, never event resolutions.

#### Scenario: Typed event resolves the awaited effect
- **GIVEN** a process worker is in event-wait for effect `E` expecting event types `T`
- **WHEN** a typed message arrives whose `type` is in `T`
- **THEN** the worker resolves effect `E` via `task:post … --status ok` with the message as the value
- **AND** removes the `.babysitter-event-wait.json` signal file
- **AND** triggers a turn so babysitter advances to the next process step

#### Scenario: Typed event that does not match is ignored
- **GIVEN** a process worker is in event-wait expecting event types `T`
- **WHEN** a typed message arrives whose `type` is NOT in `T`
- **THEN** the worker ignores it and does NOT resolve the effect

#### Scenario: Typed event arrives before the worker reaches event-wait
- **GIVEN** a process worker is NOT yet in event-wait (no signal file, no pending effect)
- **WHEN** a typed message arrives
- **THEN** the worker NAKs it so JetStream redelivers once the worker reaches its event step
- **AND** after `max_deliver` (12) attempts without entering event-wait, the event is acked and dropped

#### Scenario: Typeless chat is delivered while in event-wait without advancing the process
- **GIVEN** a process worker is parked in event-wait for effect `E`
- **WHEN** a typeless message arrives (e.g. conductor `send_message` without `ask`)
- **THEN** the worker delivers it to the LLM via `sendUserMessage(text, { deliverAs: "followUp" })`
- **AND** does NOT resolve effect `E` or remove the signal file
- **AND** babysitter's `turn_end` sees the effect still pending and keeps the process parked

#### Scenario: Typeless chat is delivered while actively mid-step
- **GIVEN** a process worker is NOT in event-wait (actively running a step)
- **WHEN** a typeless message arrives
- **THEN** the worker delivers it to the LLM immediately (it is NOT NAK'd or redelivered)

---

### Requirement: BTW Channel

The btw channel enables the conductor to ask a worker a question or request an investigation that runs in parallel with the worker's main task. The worker's response is automatically captured and published back to the conductor.

#### NATS Subject

`pinard.{vignoble}.agents.{session}.btw`

#### Payload Format

```json
{
  "message": "What branch are you working on?",
  "btw_id": "a1b2c3d4",
  "inject": false,
  "from": "conductor",
  "timestamp": "2026-05-21T20:35:08Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | yes | The question or instruction for the btw sub-session |
| `btw_id` | string | yes | Correlation ID (uuid short form) for matching replies |
| `inject` | boolean | no | If `true`, btw thread is injected into the worker's main conversation after completion |
| `from` | string | yes | Always `"conductor"` |
| `timestamp` | string | yes | ISO 8601 timestamp |

#### Scenario: Conductor asks a question
- **WHEN** the conductor calls `send_message(session, message, ask: true)`
- **THEN** the tool generates a `btw_id` (8-char uuid prefix)
- **AND** publishes to `pinard.{v}.agents.{session}.btw` with the payload
- **AND** waits for a `btw_reply` event with matching `btw_id` (timeout: 60s)
- **AND** returns the reply text to the conductor LLM

#### Scenario: Worker receives btw while busy
- **WHEN** a btw message arrives while the worker's main LLM is mid-turn
- **THEN** the worker extension triggers a pi-btw sub-session with the message
- **AND** the sub-session runs immediately in parallel (does NOT wait for the main turn to end)
- **AND** the sub-session has full tool access (read, bash, edit, write)

#### Scenario: Worker receives btw while idle
- **WHEN** a btw message arrives while the worker's main LLM is idle (between turns)
- **THEN** the worker extension still triggers a pi-btw sub-session (NOT the main channel)
- **AND** the behavior is identical to the busy case

#### Scenario: BTW with inject
- **WHEN** the btw payload has `inject: true`
- **THEN** after the btw sub-session completes, the extension calls `/btw:inject`
- **AND** the full btw thread (question + answer) is pushed into the worker's main conversation as a user message
- **AND** the main agent becomes aware of the btw exchange on its next turn
- **NOTE** The conductor typically follows up with an inbox message if action is needed

#### Scenario: BTW without inject (default)
- **WHEN** the btw payload has `inject: false` or `inject` is absent
- **THEN** the btw sub-session runs and replies
- **AND** the main agent is NOT aware of the btw exchange
- **AND** no content is injected into the main conversation

---

### Requirement: BTW Reply

The worker extension automatically publishes the btw response back to the conductor via the agent events stream.

#### NATS Subject

`pinard.{vignoble}.agents.{session}.events.btw_reply`

#### Payload Format

```json
{
  "btw_id": "a1b2c3d4",
  "response": "I'm working on branch feat/add-retry-logic",
  "timestamp": "2026-05-21T20:35:12Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `btw_id` | string | Matches the request's `btw_id` |
| `response` | string | The btw sub-session's final answer |
| `timestamp` | string | ISO 8601 timestamp of reply |

#### Scenario: Conductor receives reply
- **WHEN** a `btw_reply` event arrives with a `btw_id` matching a pending ask
- **THEN** the conductor resolves the pending `send_message(ask: true)` call
- **AND** returns the response text to the conductor LLM

#### Scenario: Reply timeout
- **WHEN** no `btw_reply` arrives within 60 seconds of the btw request
- **THEN** the conductor tool returns an error: `"BTW timeout: worker did not respond within 60s"`
- **AND** the pending ask is cleared
- **NOTE** This may happen if the worker's NATS connection is lost or pi-btw fails to start

#### Scenario: BTW sub-session fails
- **WHEN** the pi-btw sub-session encounters an error or crashes
- **THEN** the worker extension publishes a `btw_reply` with `response: "[btw error: <reason>]"`
- **AND** the `btw_id` still matches so the conductor can correlate

---

### Requirement: Interrupt Channel

The interrupt channel enables the conductor to stop a worker's current LLM turn immediately. This is stronger than a btw (which runs in parallel) and weaker than a kill (which terminates the session).

#### NATS Subject

`pinard.{vignoble}.agents.{session}.interrupt`

#### Payload Format

```json
{
  "reason": "New priority task — stop current work",
  "from": "conductor",
  "timestamp": "2026-05-21T20:35:08Z"
}
```

#### Scenario: Interrupt busy worker
- **WHEN** an interrupt message arrives while the worker's main LLM is mid-turn
- **THEN** the worker extension cancels the active LLM turn
- **AND** the reason is delivered to the worker LLM via `sendUserMessage` (so it knows why it was interrupted)
- **AND** the worker becomes idle, ready to process the next inbox message

#### Scenario: Interrupt idle worker
- **WHEN** an interrupt message arrives while the worker is idle (between turns)
- **THEN** the interrupt is a no-op (worker is already available)
- **AND** the reason is still delivered via `sendUserMessage` for context

#### Scenario: Interrupt does NOT kill the session
- **GIVEN** the interrupt channel is used
- **THEN** the worker session remains alive
- **AND** the tmux session is not killed
- **AND** the KV state is not deleted
- **AND** the worker can receive new inbox messages immediately after interrupt

---

### Requirement: Channel Selection Rules

Tools and automated dispatch SHALL route messages to the correct channel based on intent.

#### Scenario: send_message with ask: true
- **WHEN** the conductor tool `send_message` is called with `{session, message, ask: true}`
- **THEN** the message is routed to the **btw** channel
- **AND** the tool blocks until a `btw_reply` is received (or timeout)

#### Scenario: send_message with ask: true and inject: true
- **WHEN** `send_message` is called with `{session, message, ask: true, inject: true}`
- **THEN** the message is routed to the **btw** channel with `inject: true`
- **AND** after the reply, the btw thread is injected into the worker's main conversation

#### Scenario: send_message without ask
- **WHEN** `send_message` is called with `{session, message}` (no `ask` parameter)
- **THEN** the message is routed to the **main inbox** channel
- **AND** the tool returns immediately (fire-and-forget)

#### Scenario: interrupt_worker tool
- **WHEN** the conductor calls `interrupt_worker(session, reason)`
- **THEN** the message is routed to the **interrupt** channel
- **AND** the tool returns immediately

#### Scenario: dispatchToWorker automatic routing
- **WHEN** `dispatchToWorker()` fires for a dual-delivery event
- **THEN** it always publishes to the **main inbox**
- **AND** it never uses btw or interrupt

---

### Requirement: NATS Subject Conventions (addendum)

These subjects extend the conventions defined in the event-flow spec:

| Pattern | Used by |
|---------|---------|
| `pinard.{vignoble}.agents.{agentId}.process.{name}.inbox` | Conductor/daemon → process-worker inbox (typed events + typeless chat) |
| `pinard.{vignoble}.agents.{session}.btw` | Conductor → worker btw questions |
| `pinard.{vignoble}.agents.{session}.interrupt` | Conductor → worker interrupt signal |
| `pinard.{vignoble}.agents.{session}.events.btw_reply` | Worker → conductor btw response |

The btw and interrupt subjects SHALL be added to the `pinard-inboxes` stream (or a dedicated stream) so messages are durable across brief disconnections.

---

### Requirement: Concurrency Model

#### Scenario: One btw at a time per worker (v1)
- **GIVEN** the conductor sends a btw to worker X
- **WHEN** the conductor has not yet received a reply
- **THEN** the conductor SHALL NOT send another btw to worker X
- **AND** the `btw_id` field enables future concurrency without protocol changes

#### Scenario: Inbox and btw are independent
- **GIVEN** a btw sub-session is running on a worker
- **WHEN** the conductor sends a message to the worker's main inbox
- **THEN** the inbox message queues normally
- **AND** it does not interfere with the btw sub-session
- **AND** the btw reply is still published when the sub-session completes

#### Scenario: Interrupt during btw
- **GIVEN** a btw sub-session is running on a worker
- **WHEN** an interrupt arrives
- **THEN** the interrupt cancels the worker's main LLM turn only
- **AND** the btw sub-session continues (it's a separate sub-session)
- **AND** the btw reply is still published when the sub-session completes
