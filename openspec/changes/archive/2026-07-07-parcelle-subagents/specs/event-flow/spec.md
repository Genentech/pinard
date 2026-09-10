## ADDED Requirements

### Requirement: Parcelle Resolution for Events

The system SHALL resolve every agent/issue event to a destination parcelle (or the general lane) using a deterministic 3-step order.

#### Scenario: Explicit parcelle wins
- **WHEN** an event carries an explicit parcelle (issue `parcelle:<name>` label, or KV `parcelle` field for the originating session)
- **THEN** it SHALL be routed to that parcelle's overseer

#### Scenario: Default KindRepo bucket
- **WHEN** an event has no explicit parcelle but has a project that maps to a default (KindRepo) bucket
- **THEN** it SHALL be routed to that project's default parcelle overseer (parcelle name == vigne name)

#### Scenario: Fallback to general lane
- **WHEN** an event resolves to neither an explicit parcelle nor a default bucket (e.g. daemon health, scheduler meta, untriaged issue)
- **THEN** it SHALL be routed to the dashboard general lane

### Requirement: Per-Parcelle Consumer Scoping

Agent/worker subjects SHALL carry a literal `parcelles` segment followed by the parcelle id, and an overseer's durable consumer SHALL filter on its own parcelle so it receives only that parcelle's events.

#### Scenario: Subject carries the parcelles segment
- **WHEN** a worker publishes an agent event
- **THEN** the subject SHALL be `pinard.<vignoble>.parcelles.<parcelle>.agents.<session>.events.<type>`
- **AND** the parcelle SHALL be the worker's spawned parcelle (defaulting to the project)

#### Scenario: Overseer filters on its parcelle
- **WHEN** an overseer for parcelle `<name>` starts its consumer
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
- **AND** parcelle-scoped agent events SHALL be delivered to the owning overseer, not the dashboard

## MODIFIED Requirements

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
- **AND** it SHALL be consumed by the dashboard, not by a parcelle overseer
