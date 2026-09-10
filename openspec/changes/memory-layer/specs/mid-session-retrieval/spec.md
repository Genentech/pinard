## ADDED Requirements

### Requirement: Mid-Session Retrieval Architecture

Workers SHALL have access to accumulated knowledge during their session, not only at boot. A memory service abstracts the retrieval backends (Qdrant, Graphiti) behind a NATS request-reply interface.

```
Worker (any turn)
  │
  ▼ (NATS request-reply)
pinard.{vignoble}.memory.recall
  │
  ▼
Memory Service
  ├── Qdrant (fuzzy semantic: episode chunks + wiki pages)
  ├── Graphiti (structured: entity/edge chains for group_id)
  └── Blend + Summarize (cheap model)
  │
  ▼
Response: summarized context or empty
```

#### Scenario: Worker queries memory each turn
- **WHEN** the worker `message_end` hook fires with an assistant response
- **THEN** the worker extension publishes a NATS request to `pinard.{vignoble}.memory.recall`
- **AND** the request payload contains the last user message, last assistant response (truncated to 500 chars each), the `session_id`, and the `group_id`
- **AND** the worker waits up to 3 seconds for a response
- **AND** if a non-empty response is received, it is injected via `sendUserMessage` with `deliverAs: "steer"` before the next LLM turn

#### Scenario: Memory service unavailable
- **WHEN** the NATS request times out (no response within 3 seconds)
- **THEN** the worker proceeds normally without mid-session context
- **AND** no error is surfaced to the LLM

#### Scenario: Memory service returns empty
- **WHEN** the memory service determines no results meet the relevance threshold
- **THEN** it responds with `{"context": null}`
- **AND** the worker injects nothing

---

### Requirement: Query Protocol

The NATS request-reply interface SHALL follow a consistent schema for queries and responses.

#### Request payload

```json
{
  "session_id": "example-step5-a1b2c3",
  "group_id": "example-build",
  "vignoble": "genomics",
  "query": {
    "user_message": "The TileDB optimize step is failing with...",
    "assistant_excerpt": "I see an OOM error in the SLURM logs...",
    "turn_index": 12
  },
  "constraints": {
    "max_context_tokens": 400,
    "exclude_session": "example-step5-a1b2c3"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Current worker session (for dedup tracking) |
| `group_id` | string | yes | Pipeline scope for filtering |
| `vignoble` | string | yes | Vignoble name |
| `query.user_message` | string | yes | Last user/human message (truncated 500 chars) |
| `query.assistant_excerpt` | string | yes | Last assistant response (truncated 500 chars) |
| `query.turn_index` | number | yes | Monotonic turn counter |
| `constraints.max_context_tokens` | number | no | Token budget for response (default: 400) |
| `constraints.exclude_session` | string | no | Session to exclude from results (self) |

#### Response payload

```json
{
  "context": "[memory] Known issue: step5 OOMs when shards exceed 30k studies. Resolution: increase --mem-budget to 64G. Verified 2026-05-20.",
  "sources": [
    {"type": "qdrant", "id": "point-abc123", "score": 0.78},
    {"type": "graphiti", "chain": "LogPattern→Diagnosis→Action", "entities": ["step5-oom"]}
  ],
  "meta": {
    "total_candidates": 7,
    "returned_tokens": 85
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `context` | string or null | Summarized knowledge to inject. Null = nothing relevant. |
| `sources` | array | Provenance of what contributed to the response |
| `meta.total_candidates` | number | Raw hits before filtering/summarization |
| `meta.returned_tokens` | number | Approximate token count of `context` |

#### Scenario: Response format
- **WHEN** the memory service has relevant results above threshold
- **THEN** it responds with a `context` string prefixed with `[memory]`
- **AND** the context is a compressed summary, NOT raw chunks
- **AND** the token count does not exceed `max_context_tokens`

---

### Requirement: Server-Side Intelligence

The memory service SHALL perform relevance gating, summarization, and session dedup server-side. Workers are dumb clients.

#### Scenario: Relevance gating
- **WHEN** the memory service queries Qdrant and Graphiti
- **AND** the best Qdrant score is below 0.55 AND no Graphiti facts match the group_id
- **THEN** it responds with `{"context": null}`

#### Scenario: Summarization
- **WHEN** relevant results exist from one or both backends
- **THEN** the memory service uses a cheap model (Haiku) to compress hits into a single paragraph
- **AND** the summary preserves: the specific fact, the resolution/action if known, when it was last verified
- **AND** raw chunk text is never returned directly to the worker

#### Scenario: Session dedup
- **WHEN** the memory service receives a query for a session_id it has already served
- **THEN** it tracks which Qdrant point IDs and Graphiti fact IDs have already been sent to that session
- **AND** excludes previously-sent results from the current response
- **AND** the dedup state is held in memory (lost on service restart — acceptable, causes at most one repeat)

#### Scenario: Blending Qdrant and Graphiti results
- **WHEN** both Qdrant and Graphiti return relevant results
- **THEN** Graphiti results take priority (structured, higher confidence)
- **AND** Qdrant results supplement with contextual/fuzzy matches
- **AND** the summarization LLM receives both and produces a single coherent paragraph

---

### Requirement: Per-Turn Chunk Embedding (Write Path)

Worker conversation turns SHALL be embedded into Qdrant in near-real-time, independently of the session-end episode flow to Graphiti.

```
Worker turn N (message_end hook)
  │
  ▼
NATS: pinard.{vignoble}.memory.chunks
  │
  ▼
Chunk Embedder Service
  - embed(content) → vector
  - upsert into Qdrant
  - available for retrieval within seconds
```

#### Scenario: Worker publishes chunk on each turn
- **WHEN** the worker `message_end` hook fires with an assistant message containing text (not tool-only)
- **THEN** the worker publishes to `pinard.{vignoble}.memory.chunks`
- **AND** the payload includes: `session_id`, `group_id`, `turn_index`, `role` ("assistant"), `content` (truncated to 2000 chars), and `context` (project, step if known)

#### Scenario: Chunk payload format

```json
{
  "session_id": "example-step5-a1b2c3",
  "group_id": "example-build",
  "vignoble": "genomics",
  "turn_index": 12,
  "role": "assistant",
  "content": "The OOM is caused by shard 47 exceeding the 30k study limit...",
  "context": {
    "project": "example-workers",
    "step": "step5-optimize"
  },
  "timestamp": "2026-05-25T14:32:00Z"
}
```

#### Scenario: Chunk embedder processes and upserts
- **WHEN** the chunk embedder receives a message from `memory.chunks`
- **THEN** it embeds the content using the configured embedding model
- **AND** upserts into Qdrant with payload: `{group_id, session_id, turn_index, timestamp, source: "episode_chunk", step, project}`
- **AND** checks for near-duplicate (cosine > 0.95 with same `group_id`) before inserting
- **AND** acknowledges the NATS message

#### Scenario: Chunk embedder is independent of episode ingester
- **WHEN** the chunk embedder processes per-turn chunks
- **THEN** it operates independently of the Graphiti episode ingester
- **AND** it does NOT call Graphiti — it only writes to Qdrant
- **AND** it uses the same embedding model as the wiki continuous ingest (for consistent vector space)

#### Scenario: Near-duplicate suppression
- **WHEN** the chunk embedder generates a vector for a new chunk
- **THEN** it searches Qdrant for existing points with cosine > 0.95 within the same `group_id`
- **AND** if a near-duplicate exists, the chunk is dropped (not upserted)
- **AND** the NATS message is still acknowledged (the information already exists)

---

### Requirement: NATS Subject Conventions (Memory Retrieval)

| Pattern | Direction | Used by |
|---------|-----------|---------|
| `pinard.{vignoble}.memory.recall` | request-reply | Worker → Memory Service |
| `pinard.{vignoble}.memory.chunks` | publish (JetStream) | Worker → Chunk Embedder |

#### Scenario: Stream definition for chunks
- **WHEN** the Pinard NATS infrastructure is provisioned
- **THEN** the `pinard-memory` stream (already defined) SHALL also cover `pinard.{vignoble}.memory.chunks`
- **AND** the durable consumer for the chunk embedder SHALL be `pinard-memory-chunks-{vignoble}`
- **AND** retention is `limits` with 7-day max age (same as episodes)

#### Scenario: Recall is request-reply (not stream)
- **WHEN** a worker queries `memory.recall`
- **THEN** it uses core NATS request-reply (not JetStream)
- **AND** there is no persistence — if the memory service is down, the request times out and the worker proceeds

---

### Requirement: Memory Service Deployment

The memory service SHALL run as a standalone process alongside the episode ingester.

#### Scenario: Single process handles both recall and chunk embedding
- **WHEN** the memory service starts
- **THEN** it subscribes to `pinard.{vignoble}.memory.recall` for request-reply
- **AND** it subscribes as a durable consumer to `pinard.{vignoble}.memory.chunks` for embedding
- **AND** it connects to Qdrant (for vector upsert/search)
- **AND** it connects to Graphiti/FalkorDB (for structured queries)
- **AND** it configures the summarization LLM (Haiku) for response compression

#### Scenario: Memory service resilience
- **WHEN** Qdrant is unavailable
- **THEN** recall queries fall back to Graphiti-only results
- **AND** chunk embedding messages are NAKed with backoff until Qdrant recovers

- **WHEN** Graphiti is unavailable
- **THEN** recall queries fall back to Qdrant-only results

- **WHEN** both are unavailable
- **THEN** recall queries respond with `{"context": null}`
- **AND** chunk messages queue in JetStream until services recover

#### Scenario: LLM unavailability for summarization
- **WHEN** the summarization LLM (Haiku) is unavailable
- **THEN** the memory service returns the top Graphiti fact verbatim (no summarization)
- **OR** the top Qdrant chunk text truncated to `max_context_tokens`
- **AND** this is a degraded mode — accuracy without polish

---

### Requirement: Observability

#### Scenario: Memory service status file
- **WHEN** the memory service processes queries
- **THEN** it updates `{vignoble}/logs/memory-service-status.json` with:

```json
{
  "state": "running",
  "qdrant_available": true,
  "graphiti_available": true,
  "queries_today": 142,
  "queries_with_results": 89,
  "chunks_embedded_today": 67,
  "avg_query_latency_ms": 230,
  "dedup_sessions_tracked": 4
}
```

#### Scenario: Query logging
- **WHEN** the memory service responds to a recall query
- **THEN** it logs to `{vignoble}/logs/memory-service.log`: timestamp, session_id, group_id, qdrant_hits, graphiti_hits, response_tokens, latency_ms, was_empty (boolean)
