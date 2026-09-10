## ADDED Requirements

### Requirement: Architecture Overview

The memory layer SHALL be composed of four independent subsystems connected via NATS. Workers MUST NOT interact with Graphiti directly.

```
[Worker ↔ Human]  ──message_end hook──►  [NATS: memory.episodes]
                                                │
                                          [Episode Ingester]
                                                │
                                                ▼
                                          [Graphiti (FalkorDB)]
                                                │
                              ┌─────────────────┴──────────────────┐
                              ▼                                    ▼
                    [NATS: memory.query]              [Recipe Exporter (cron)]
                    (request-reply at boot)                     │
                              │                                 ▼
                              ▼                          [recipes/{group_id}/
                    [Worker context injection]            current.yaml → git]
```

#### Scenario: Worker isolation from Graphiti
- **WHEN** a worker publishes a conversation transcript
- **THEN** it publishes to `pinard.{vignoble}.memory.episodes` via NATS
- **AND** the worker has no direct dependency on Graphiti, FalkorDB, or the episode ingester

#### Scenario: Episode ingester as independent service
- **WHEN** the episode ingester starts
- **THEN** it connects to NATS and subscribes as a durable consumer on the `pinard-memory` stream
- **AND** it connects to Graphiti (FalkorDB + embedding service)
- **AND** it operates independently of any conductor or worker process lifecycle

#### Scenario: Recipe injection at boot
- **WHEN** a worker boots and connects to NATS
- **THEN** it queries the recipe store for its pipeline's `group_id`
- **AND** relevant recipes are injected into the worker's system prompt before the first LLM turn

---

### Requirement: NATS Subject Conventions

All memory-related subjects SHALL follow established Pinard naming conventions.

| Pattern | Used by |
|---------|---------|
| `pinard.{vignoble}.memory.episodes` | Worker/conductor transcript publish |
| `pinard.{vignoble}.memory.query` | Request-reply for recipe retrieval at boot |
| `pinard.{vignoble}.memory.teaching.{session}` | Teaching mode activation signals |

#### Scenario: Stream definition
- **WHEN** the Pinard NATS infrastructure is provisioned
- **THEN** a JetStream stream `pinard-memory` SHALL exist covering subjects `pinard.{vignoble}.memory.>`
- **AND** retention policy SHALL be `limits` with a 7-day max age
- **AND** the durable consumer for the episode ingester SHALL be `pinard-memory-ingester-{vignoble}`

---

### Requirement: Episode Payload Format

Transcripts published to the memory stream SHALL follow a consistent JSON schema.

```json
{
  "session_id": "example-step4-a1b2c3",
  "group_id": "example-build",
  "vignoble": "genomics",
  "source": "worker",
  "mode": "normal",
  "timestamp": "2026-05-25T14:32:00Z",
  "episode": {
    "role": "assistant",
    "content": "The TileDB optimize step is OOMing on the xlarge pass...",
    "turn_index": 42
  },
  "context": {
    "project": "example-workers",
    "step": "step5-optimize",
    "tools_used": ["bash", "read"]
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Worker session name |
| `group_id` | string | yes | Pipeline scope (e.g., `example-build`) |
| `vignoble` | string | yes | Vignoble name for multi-tenant isolation |
| `source` | enum | yes | `"worker"` or `"conductor"` |
| `mode` | enum | yes | `"normal"` or `"teaching"` |
| `timestamp` | string | yes | ISO 8601 timestamp |
| `episode.role` | enum | yes | `"user"`, `"assistant"`, or `"system"` |
| `episode.content` | string | yes | The transcript text |
| `episode.turn_index` | number | yes | Monotonic turn counter within the session |
| `context` | object | no | Optional metadata for extraction hints |

#### Scenario: Worker publishes transcript on message_end
- **WHEN** the worker `message_end` hook fires with an assistant message
- **THEN** the worker extension publishes to `pinard.{vignoble}.memory.episodes`
- **AND** the payload includes the session's `group_id` from `WORKER_GROUP_ID` env var
- **AND** the `mode` field is `"normal"` unless teaching mode is active

#### Scenario: Empty or tool-only messages are not published
- **WHEN** the assistant's response contains only tool calls with no text content
- **THEN** no episode is published for that turn

---

### Requirement: Episode Ingester

The episode ingester SHALL consume episodes from NATS and call `graphiti.add_episode()` with the appropriate ontology.

#### Scenario: Normal mode ingestion
- **WHEN** the ingester receives an episode with `mode: "normal"`
- **THEN** it calls `graphiti.add_episode()` with the episode content
- **AND** it passes the prescribed entity types and edge types from the ontology registry
- **AND** it uses the cheap extraction model (Haiku or 4o-mini)
- **AND** `group_id` is passed as the Graphiti `group_id` parameter
- **AND** only prescribed entity types are used (no learned type emergence)

#### Scenario: Teaching mode ingestion
- **WHEN** the ingester receives an episode with `mode: "teaching"`
- **THEN** it calls `graphiti.add_episode()` with `source_description: "teaching_session"`
- **AND** both prescribed and learned entity types are enabled
- **AND** novel entities that don't match prescribed types are stored with `ontology_tier: "learned"` metadata

#### Scenario: LLM becomes unavailable mid-drain
- **WHEN** the ingester is draining episodes and `graphiti.add_episode()` fails because the Anthropic API returns 401/403 (token expired mid-session)
- **THEN** the current NATS message is NAKed with a 5-minute delay
- **AND** the ingester transitions to polling sleep mode (stop pulling messages, probe every 5 minutes)
- **AND** it does NOT continue pulling and NAKing further messages (avoids a NAK storm)
- **AND** when the next poll succeeds, it transitions back to draining mode and the NAKed message is redelivered naturally

#### Scenario: Extraction failure dead-letter
- **WHEN** `graphiti.add_episode()` fails with a non-LLM error (FalkorDB timeout, schema validation error, malformed response)
- **THEN** the NATS message is NAKed with short backoff (10s → 30s → 60s)
- **AND** after 5 failed attempts the message is terminated to `pinard.{vignoble}.memory.dead`

#### Scenario: Idempotency
- **WHEN** the same episode is delivered twice (NATS redelivery)
- **THEN** Graphiti's internal deduplication prevents duplicate entities
- **AND** the ingester acks the message after successful `add_episode()` return

---

### Requirement: Ontology Model — Entity Types

The knowledge graph SHALL use Pydantic models to define prescribed entity types that guide Graphiti's LLM-based extraction.

| Entity Type | Description | Example |
|-------------|-------------|---------|
| `PipelineStep` | A discrete step in a data pipeline | `step5-optimize` |
| `DataAsset` | A persistent artifact produced or consumed | `gwas_shard_47` |
| `SlurmJob` | A compute job submitted to a scheduler | `optimize_chained` |
| `LogPattern` | A recognizable log output signaling a condition | `"OOM killed process"` |
| `Diagnosis` | A root cause explanation for a failure | `"shard exceeds memory limit"` |
| `Action` | A concrete operational response | `"increase --mem-budget"` |
| `EnvironmentCondition` | A prerequisite that must hold | `"Redis SIF image present"` |
| `ThresholdDecision` | A numeric boundary triggering different behavior | `"30k studies per shard"` |

#### Scenario: Prescribed types guide extraction
- **WHEN** the episode ingester processes any episode
- **THEN** it provides all prescribed entity types to `graphiti.add_episode()` via the `entity_types` parameter
- **AND** Graphiti's extraction LLM uses these models as schema hints

#### Scenario: Entity attributes populated from context
- **WHEN** Graphiti extracts an entity matching a prescribed type
- **THEN** it populates the model's fields from the episode content
- **AND** fields not mentioned in the text are left at their defaults

---

### Requirement: Ontology Model — Edge Types

Relationships between entities SHALL be constrained by typed edge definitions and an edge type map.

| Edge Type | Source → Target | Description |
|-----------|----------------|-------------|
| `DependsOn` | `PipelineStep` → `PipelineStep` | Sequential dependency |
| `Produces` | `PipelineStep` → `DataAsset` | Step creates an artifact |
| `Consumes` | `PipelineStep` → `DataAsset` | Step reads an artifact |
| `IndicatesProblem` | `LogPattern` → `Diagnosis` | Log signals root cause |
| `ResolvedBy` | `Diagnosis` → `Action` | Diagnosis fixed by action |
| `RequiresCondition` | `PipelineStep` → `EnvironmentCondition` | Step needs precondition |
| `TriggersDecision` | `PipelineStep` → `ThresholdDecision` | Step triggers branching |

#### Scenario: Edge type map constrains extraction
- **WHEN** Graphiti extracts a relationship from an episode
- **THEN** the episode ingester passes the `edge_type_map` to `graphiti.add_episode()`
- **AND** Graphiti only creates edges that match the allowed source→target pairs

#### Scenario: Edge attributes capture provenance
- **WHEN** an edge is extracted from a teaching session
- **THEN** the `learned_from` field is set to `"teaching"`
- **WHEN** an edge is extracted from an autonomous run
- **THEN** `learned_from` is set to `"autonomous"`

---

### Requirement: Ontology Evolution Cycle

The ontology SHALL support a three-phase lifecycle: prescribed → learned → promoted.

#### Scenario: Learned type emergence (teaching mode only)
- **WHEN** the ingester processes a teaching-mode episode
- **AND** Graphiti extracts an entity that does not match any prescribed type
- **THEN** the entity is stored with `ontology_tier: "learned"` metadata
- **AND** it is assigned a provisional type name based on LLM classification

#### Scenario: Learned type accumulation
- **WHEN** multiple episodes produce entities of the same learned type
- **THEN** the type's instance count is tracked in Graphiti metadata
- **AND** the recipe exporter includes learned types with 3+ instances in a `proposed_types` section

#### Scenario: Promotion to prescribed
- **WHEN** a human adds a Pydantic model to the ontology registry for a learned type
- **THEN** existing learned entities of that type are re-tagged as `ontology_tier: "prescribed"`
- **AND** the edge type map is updated to include valid edges for the new type

#### Scenario: Suppression of invalid learned types
- **WHEN** a human adds a type name to the `suppressed_types` list
- **THEN** future extraction skips entities of that type
- **AND** existing entities are soft-deleted with `valid_until: <now>`

---

### Requirement: Cross-Pipeline Scoping

All memory operations SHALL be scoped by `group_id` to prevent cross-pipeline knowledge bleeding.

#### Scenario: Episode scoped by group_id
- **WHEN** a worker publishes an episode
- **THEN** the `group_id` field MUST be present in the payload
- **AND** Graphiti stores entities and edges under that `group_id`

#### Scenario: Query scoped by group_id
- **WHEN** a worker queries recipes at boot
- **THEN** only facts for the worker's `group_id` are returned

#### Scenario: Entity deduplication within group
- **WHEN** Graphiti deduplicates entities
- **THEN** deduplication only considers entities within the same `group_id`
- **AND** two entities named "step3" in different groups remain separate

#### Scenario: Multi-worker knowledge sharing within a group
- **WHEN** multiple workers publish episodes with the same `group_id`
- **THEN** the knowledge graph connects them through shared entities
- **AND** a replacement worker inherits knowledge from all workers in its group

---

### Requirement: Temporal Validity

Facts in the knowledge graph SHALL have temporal validity windows.

#### Scenario: Fact creation with valid_from
- **WHEN** a new entity or edge is extracted from an episode
- **THEN** it is stored with `valid_from` set to the episode's timestamp
- **AND** `valid_until` is null (fact is currently valid)

#### Scenario: Fact superseded by contradiction
- **WHEN** Graphiti detects that a new fact contradicts an existing fact
- **AND** the resolution is "replace"
- **THEN** the old fact's `valid_until` is set to the new fact's `valid_from`
- **AND** the old fact is excluded from boot injection and git export

#### Scenario: Historical query for post-mortem
- **WHEN** a query includes a time range parameter
- **THEN** expired facts within that range are included in results
- **AND** results are annotated with their validity windows

---

### Requirement: Cost Model

The memory layer SHALL use model tiers appropriate to each operation's complexity.

| Operation | Model tier | Rationale |
|-----------|-----------|-----------|
| Episode extraction | Cheap: Haiku or 4o-mini | Runs on every episode; volume-sensitive |
| Worker execution | Standard: Sonnet | Existing worker model, unchanged |
| Conductor orchestration | Premium: Opus | Existing conductor model, unchanged |
| Contradiction resolution | Cheap: same as extraction | Triggered by Graphiti internally |

#### Scenario: Extraction model configuration
- **WHEN** the episode ingester initializes Graphiti
- **THEN** the extraction LLM is configured via `MEMORY_EXTRACTION_MODEL` env var
- **AND** defaults to `claude-haiku-4-5-20251001` if not set

#### Scenario: Worker model unchanged
- **WHEN** a worker processes tasks with injected recipes
- **THEN** it uses its normal model (Sonnet) regardless of memory layer operations

---

### Requirement: Ingester Resilience

The episode ingester SHALL tolerate intermittent LLM availability and make progress whenever the LLM is reachable.

#### Scenario: Startup LLM probe
- **WHEN** the episode ingester starts
- **THEN** it fetches the API token from `MEMORY_TOKEN_URL`
- **AND** configures the Anthropic client with the fetched token
- **AND** makes a lightweight probe call to the Anthropic API to verify token validity
- **AND** if the probe succeeds, `llm_available` is set to `true` and episode draining begins
- **AND** if the probe returns 401/403 (token expired — user not logged in), it logs "LLM token expired, will retry in 5 minutes" and enters polling sleep
- **AND** the token URL itself MUST NOT be logged (it is a secret)

#### Scenario: Polling sleep rate limiting
- **WHEN** the ingester is in polling sleep (LLM unavailable)
- **THEN** it fetches the token from `MEMORY_TOKEN_URL` at most once every 5 minutes
- **AND** between polls the ingester sleeps and does NOT consume messages from the NATS consumer
- **AND** no messages are NAKed during polling sleep — the consumer simply does not pull
- **AND** when a poll succeeds (probe returns 200), the ingester transitions to draining mode

#### Scenario: Backlog drain on LLM availability
- **WHEN** the LLM becomes available after a period of unavailability
- **THEN** the ingester drains pending episodes oldest-first from the JetStream consumer
- **AND** processing continues until the queue is empty or the LLM becomes unavailable again

#### Scenario: LLM goes offline mid-drain
- **WHEN** the ingester is processing episodes and the LLM becomes unavailable
- **THEN** the current episode is NAKed with 5-minute backoff
- **AND** the ingester returns to polling sleep mode
- **AND** no episodes are lost (JetStream retains them)

---

### Requirement: Observability

The memory layer SHALL expose operational status and logs for monitoring progress over time.

#### Scenario: Ingester status file
- **WHEN** the episode ingester completes a processing cycle (successful extraction, failed extraction, or LLM probe)
- **THEN** it updates `{vignoble}/logs/memory-ingester-status.json` with:

```json
{
  "state": "draining",
  "llm_available": true,
  "queue_depth": 47,
  "last_extraction_at": "2026-05-25T14:32:00Z",
  "extracted_today": 23,
  "errors_today": 1,
  "oldest_pending_age_hours": 18.5
}
```

| Field | Type | Description |
|-------|------|-------------|
| `state` | enum | `"draining"` (processing), `"waiting"` (LLM unavailable), `"idle"` (queue empty) |
| `llm_available` | boolean | Whether the LLM endpoint is currently reachable |
| `queue_depth` | number | Pending episodes in the JetStream consumer |
| `last_extraction_at` | string | ISO 8601 timestamp of last successful extraction |
| `extracted_today` | number | Episodes successfully extracted in the last 24h |
| `errors_today` | number | Extraction failures (non-LLM) in the last 24h |
| `oldest_pending_age_hours` | number | Age of the oldest unprocessed episode |

#### Scenario: Conductor dashboard integration
- **WHEN** the conductor TUI dashboard refreshes
- **THEN** it reads `{vignoble}/logs/memory-ingester-status.json`
- **AND** displays a one-line summary: `Memory: {queue_depth} queued, last extraction {relative_time}` when draining/idle
- **AND** displays `Memory: LLM offline, {queue_depth} queued ({oldest_pending_age_hours}h oldest)` when waiting

#### Scenario: Ingestion logging
- **WHEN** the episode ingester processes an episode
- **THEN** it logs to `{vignoble}/logs/memory-ingester.log`: timestamp, session_id, group_id, mode, entities_extracted count, edges_extracted count, duration_ms

#### Scenario: Extraction failure logging
- **WHEN** episode extraction fails or produces zero entities from a teaching-mode episode
- **THEN** it is logged with level WARN including episode content truncated to 200 chars

#### Scenario: Export logging
- **WHEN** the recipe exporter runs
- **THEN** it logs to `{vignoble}/logs/memory-export.log`: groups processed, facts per group, commit SHA (if committed), duration
