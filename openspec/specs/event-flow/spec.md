## Purpose

This specification documents the complete event routing between all pinard components, identifying sources, destinations, and dual-delivery paths that require careful handling to avoid feedback loops.

## Components

| Component | Role |
|-----------|------|
| **Worker** | Background coding agent (one per task) |
| **Conductor** | Orchestrator agent managing all workers |
| **MR watcher** | Polls GitLab for MR state changes, pipeline status, review comments |
| **Issue watcher** | Polls GitLab for new issues labeled `pinard` |
| **Scheduler** | Evaluates cron schedules, spawns agents |
## Requirements
### Requirement: Event Routing Matrix

The system SHALL route events according to the following matrix. Each row represents one event flow from source to destination.

| Source | Event | Destination | Delivery mechanism |
|--------|-------|-------------|-------------------|
| Worker | `agent_idle` | Conductor LLM | NATS stream → `sendUserMessage` |
| Worker | `session_ended` | Conductor LLM | NATS stream → `sendUserMessage` |
| Worker | `notification` | Conductor LLM | `aoc_notify` tool → `aoc notify` → NATS `notifications` stream → `sendUserMessage` |
| Worker | `btw_reply` | Conductor LLM | NATS stream → resolves pending `send_message(ask: true)` call |
| MR watcher | `mr_merged` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `mr_closed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `auto_merged` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `pipeline_failed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `pipeline_failed` | Worker LLM | Conductor dispatches → inbox |
| MR watcher | `pipeline_passed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `review_comment` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `review_comment` | Worker LLM | Conductor dispatches → inbox |
| MR watcher | `needs_approval` | Conductor LLM | NATS stream → `sendUserMessage` (ACK_REQUIRED) |
| MR watcher | `circuit_breaker` | Conductor LLM | NATS stream → `sendUserMessage` (ACK_REQUIRED) |
| MR watcher | `main_pipeline_passed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `main_pipeline_failed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `main_pipeline_failed` | Worker LLM | Conductor dispatches → inbox |
| MR watcher | `tag_pipeline_passed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `tag_pipeline_failed` | Conductor LLM | NATS stream → `sendUserMessage` |
| MR watcher | `tag_pipeline_failed` | Worker LLM | Conductor dispatches → inbox |
| Issue watcher | `issues_new` | Conductor LLM | NATS stream → `sendUserMessage` |
| Scheduler | `schedule_spawned` | Conductor LLM | NATS stream → `sendUserMessage` (ACK_REQUIRED) |
| Scheduler | `schedule_skipped` | Conductor LLM | NATS stream → `sendUserMessage` (ACK_REQUIRED) |
| Scheduler | `schedule_failed` | Conductor LLM | NATS stream → `sendUserMessage` (ACK_REQUIRED) |

### Requirement: Dual-Delivery Events

Events dispatched to BOTH the conductor LLM and a worker inbox SHALL be clearly identified. These are the only events where two LLMs receive the same underlying information:

- `pipeline_failed`
- `review_comment`
- `main_pipeline_failed`
- `tag_pipeline_failed`

#### Scenario: Conductor receives a dispatch-type event
- **WHEN** the conductor receives an event whose type is in the dispatch set
- **THEN** it SHALL format and deliver the event to the conductor LLM via `sendUserMessage`
- **AND** it SHALL dispatch an actionable message to the worker's inbox (`pinard.{vignoble}.agents.{session}.inbox`)
- **AND** the conductor LLM message SHOULD be informational only (no action expected from conductor)
- **AND** the worker inbox message SHOULD contain instructions for the worker to act

### Requirement: No Self-Delivery

A worker SHALL NOT receive events it published itself.

#### Scenario: Worker publishes agent_idle
- **WHEN** a worker publishes `agent_idle` to the event stream
- **THEN** the conductor SHALL NOT dispatch that event back to the same worker's inbox
- **AND** only the conductor LLM receives the notification

#### Scenario: Worker-originated events excluded from dispatch
- **GIVEN** the dispatch set is `{pipeline_failed, review_comment, main_pipeline_failed, tag_pipeline_failed}`
- **AND** the worker only publishes `{agent_idle, session_ended}`
- **THEN** there is no overlap between worker-published event types and dispatch-type events
- **AND** no feedback loop can occur from worker self-delivery

### Requirement: Events NOT Dispatched to Workers

The following events SHALL NOT be dispatched to any worker inbox. They are for the conductor LLM only:

- `agent_idle` — informational, worker already knows it's idle
- `session_ended` — worker is already gone
- `mr_merged` — no action for the worker (session will be killed)
- `mr_closed` — no action for the worker (session will be killed)
- `auto_merged` — no action for the worker (session will be killed)
- `pipeline_passed` — informational success, no action needed
- `main_pipeline_passed` — informational success
- `tag_pipeline_passed` — informational success
- `needs_approval` — human action required, not worker
- `circuit_breaker` — worker is being killed, not instructed
- `issues_new` — conductor decides whether to spawn, not an existing worker
- `schedule_spawned` / `schedule_skipped` / `schedule_failed` — conductor awareness only

### Requirement: NATS Subject Conventions

All events SHALL follow these subject patterns. Agent/worker subjects are parcelle-scoped via a literal `parcelles` segment; vignoble-level subjects are not parcelle-scoped:

| Pattern | Used by |
|---------|---------|
| `pinard.{vignoble}.parcelles.{parcelle}.agents.{session}.events.{type}` | Agent events (from workers and MR watcher) |
| `pinard.{vignoble}.parcelles.{parcelle}.agents.{session}.inbox` | Worker inbox — main channel (conductor → worker) |
| `pinard.{vignoble}.parcelles.{parcelle}.agents.{session}.btw` | BTW channel — parallel questions (conductor → worker) |
| `pinard.{vignoble}.parcelles.{parcelle}.agents.{session}.interrupt` | Interrupt channel — cancel current turn (conductor → worker) |
| `pinard.{vignoble}.issues.new` | Issue watcher events (vignoble-level; dashboard) |
| `pinard.{vignoble}.schedules.{name}.{status}` | Scheduler events (vignoble-level; dashboard) |
| `pinard.{vignoble}.notifications` | Worker → conductor notifications (vignoble-level; dashboard) |

Agent events always carry a parcelle (a worker is spawned with `--parcelle`, defaulting to the project). Parcelle-less events (issues before triage, schedules, notifications, daemon health) remain vignoble-level and are the dashboard's domain. `_general` is reserved for the dashboard's own session, not a wire parcelle. Stream subject wildcards for agent events SHALL be `pinard.*.parcelles.*.agents.*.events.>`, and this restructuring SHALL be cut over as a versioned change (bump stream/consumer names so stale durables are not reused).

See `openspec/specs/agent-channels/spec.md` for full channel semantics.

#### Scenario: Agent subjects are parcelle-scoped
- **WHEN** any agent event, inbox, btw, or interrupt subject is constructed
- **THEN** it SHALL include the `parcelles.{parcelle}` segment as shown above

#### Scenario: Vignoble-level subjects are not parcelle-scoped
- **WHEN** an issue, schedule, or notification subject is constructed
- **THEN** it SHALL remain at the vignoble level (no `parcelles` segment)
- **AND** it SHALL be consumed by the dashboard, not by a parcelle maître

### Requirement: ACK_REQUIRED Events (Conductor Only)

Events marked ACK_REQUIRED SHALL be held in the **conductor's** pending queue with a heartbeat (`msg.working()`) until explicitly acknowledged by the human via the `ack_event` tool. These events represent decisions or situations requiring human judgment:

- `schedule_spawned`
- `schedule_skipped`
- `schedule_failed`
- `needs_approval`
- `circuit_breaker`

ACK_REQUIRED applies exclusively to the conductor's event stream consumption. It does NOT apply to the worker inbox.

### Requirement: Worker Inbox Has No Human Gating

Messages delivered to a worker's inbox (`pinard.{vignoble}.agents.{session}.inbox`) SHALL be forwarded immediately to the worker LLM via `sendUserMessage` with no human approval step. The worker acts autonomously on all inbox messages.

This is intentional: dispatch-type events (`pipeline_failed`, `review_comment`) are actionable instructions that the worker should execute without waiting for human confirmation.

### Requirement: Notification Channel (Worker → Conductor)

The worker SHALL have a tool (`aoc_notify`) to send freeform messages to the conductor. These notifications flow through a separate channel from agent events. The worker's notification tool SHALL NOT display tmux messages or ring terminal bells — it communicates exclusively via NATS.

#### Scenario: Worker sends a notification
- **WHEN** the worker LLM calls the `aoc_notify` tool with a message
- **THEN** the tool SHALL:
  - Publish to `pinard.{vignoble}.notifications` via NATS
  - Log to `notifications.log`
- **AND** it SHALL NOT display tmux messages or ring bells (those are conductor-only via `notify_user`)
- **AND** the conductor receives the notification via its `pinard-notifications` consumer
- **AND** the conductor delivers it to its LLM via `sendUserMessage`

#### Scenario: Notification does NOT loop back to the worker
- **WHEN** the conductor LLM receives a notification from a worker
- **THEN** it SHALL NOT automatically dispatch it back to the same worker's inbox
- **NOTE** The conductor LLM MAY choose to manually instruct a worker (via `send_message` tool), which could create conversational ping-pong. This is a conductor LLM judgment call, not a system-level feedback loop.

### Requirement: Tool Ownership

Each tool SHALL be registered only on the role that needs it. Workers SHALL NOT have access to conductor-only tools.

#### Conductor-only tools

| Tool | Purpose |
|------|---------|
| `spawn_agent` | Create new worker sessions |
| `list_workers` | Query KV for all worker states |
| `kill_worker` | Terminate a worker session |
| `send_message` | Send freeform message to a worker's inbox |
| `get_notifications` | Read notification log |
| `get_schedules` | Show schedule configuration |
| `create_schedule` | Create a new schedule |
| `get_watcher_logs` | Read watcher log files |
| `get_agent_events` | Query NATS event history |
| `ack_event` | Acknowledge ACK_REQUIRED events |
| `interrupt_worker` | Cancel a worker's current turn without killing the session |

#### Worker-only tools

| Tool | Purpose |
|------|---------|
| `aoc_notify` | Send notification to conductor via NATS (no tmux) |

#### Shared tools (both conductor and worker)

| Tool | Purpose |
|------|---------|
| `read_issue` | Read GitLab issue details |
| `create_issue` | Create GitLab issues |
| `link_issues` | Link issues to an MR |
| `track_mr` | Register MR with the watcher |
| `create_cuvee` | Create a new cuvée |
| `open_cuvee_mr` | Open a cuvée merge request |
| `git` | Git operations |

#### Scenario: Worker must not see conductor tools
- **GIVEN** a worker session is started
- **THEN** the worker extension SHALL register only worker-only and shared tools
- **AND** the conductor extension SHALL NOT register its tools in worker sessions
- **AND** the worker SHALL have no way to spawn, list, or kill other workers

### Requirement: Single Communication Channel

All communication between conductor and worker SHALL use NATS as the sole transport. There is no tmux send-keys, shared file, or direct function call between the two.

#### Conductor → Worker (inbox)

Three senders, one subject (`pinard.{vignoble}.agents.{session}.inbox`):

| Sender | Trigger | Message content |
|--------|---------|-----------------|
| `dispatchToWorker()` | Automatic on `pipeline_failed`, `review_comment`, `*_pipeline_failed` | Hardcoded template with instructions |
| `send_message` tool | Conductor LLM decides to call it | Freeform — whatever the LLM writes |
| `/send` command | Human types in conductor UI | Freeform — whatever the human types |

All three publish to the same NATS subject. The worker's inbox consumer delivers the message to its LLM indiscriminately — it has no way to distinguish the sender.

#### Worker → Conductor

| Mechanism | Subject | What conductor sees |
|-----------|---------|---------------------|
| Agent events | `pinard.{v}.agents.{session}.events.{type}` | `sendUserMessage` to conductor LLM |
| KV state | `pinard-agents` bucket, key = session name | Conductor watches for status changes |
| `aoc_notify` tool | `pinard.{v}.notifications` | `sendUserMessage` to conductor LLM |

### Requirement: Parcelle Resolution for Events

The system SHALL resolve every agent/issue event to a destination parcelle (or the general lane) using a deterministic 3-step order.

#### Scenario: Explicit parcelle wins
- **WHEN** an event carries an explicit parcelle (issue `parcelle:<name>` label, or KV `parcelle` field for the originating session)
- **THEN** it SHALL be routed to that parcelle's maître

#### Scenario: Default KindRepo bucket
- **WHEN** an event has no explicit parcelle but has a project that maps to a default (KindRepo) bucket
- **THEN** it SHALL be routed to that project's default parcelle maître (parcelle name == vigne name)

#### Scenario: Fallback to general lane
- **WHEN** an event resolves to neither an explicit parcelle nor a default bucket (e.g. daemon health, scheduler meta, untriaged issue)
- **THEN** it SHALL be routed to the dashboard general lane

### Requirement: Per-Parcelle Consumer Scoping

Agent/worker subjects SHALL carry a literal `parcelles` segment followed by the parcelle id, and a maître's durable consumer SHALL filter on its own parcelle so it receives only that parcelle's events.

#### Scenario: Subject carries the parcelles segment
- **WHEN** a worker publishes an agent event
- **THEN** the subject SHALL be `pinard.<vignoble>.parcelles.<parcelle>.agents.<session>.events.<type>`
- **AND** the parcelle SHALL be the worker's spawned parcelle (defaulting to the project)

#### Scenario: Maître filters on its parcelle
- **WHEN** a maître for parcelle `<name>` starts its consumer
- **THEN** the durable consumer SHALL use `filter_subject` `pinard.<vignoble>.parcelles.<name>.agents.*.events.>`
- **AND** it SHALL NOT receive any other parcelle's events

#### Scenario: Literal segment avoids collisions
- **WHEN** a parcelle is named the same as a fixed top-level token (e.g. `issues`)
- **THEN** the `parcelles` segment SHALL keep its subjects distinct from `pinard.<vignoble>.issues.>`
- **AND** no subject collision SHALL occur

### Requirement: Dashboard Consumes Overview Only

The dashboard consumer SHALL NOT receive the per-parcelle agent-event firehose.

#### Scenario: Firehose not delivered to dashboard
- **WHEN** parcelle-scoped agent events are published
- **THEN** the dashboard SHALL consume only a lightweight overview (counts/alerts/notifications)
- **AND** parcelle-scoped agent events SHALL be delivered to the owning maître, not the dashboard

