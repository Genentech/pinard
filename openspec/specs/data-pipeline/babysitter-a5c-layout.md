# Babysitter Run Directory Layout

Reference for the on-disk state produced by a babysitter run.

Runs are stored in the vignoble under `parcelles/<parcelle>/runs/<runId>/`. The run ID is deterministic when issue-driven (`project-process-issueIID`) or session-based for one-offs.

## Directory structure

```
<vignoble>/parcelles/<parcelle>/runs/<runId>/
├── run.json                          ← Run metadata (identity, entrypoint, completion proof)
├── state/
│   ├── state.json                    ← Derived cache of effect statuses (rebuilt from journal)
│   └── output.json                   ← Process return value (written on RUN_COMPLETED)
├── journal/
│   ├── 000001.<ulid>.json            ← RUN_CREATED
│   ├── 000002.<ulid>.json            ← EFFECT_REQUESTED (task dispatched)
│   ├── 000003.<ulid>.json            ← EFFECT_RESOLVED (task:post received)
│   ├── 000004.<ulid>.json            ← RUN_COMPLETED (process function returned)
│   └── ...                           ← Append-only, checksummed, monotonic sequence
└── tasks/<effectId>/
    ├── task.json                     ← Task definition (kind, prompt, schema, inputs)
    └── result.json                   ← Posted result (value, status, timestamps)
```

## IDs

All IDs are **ULIDs** (Universally Unique Lexicographically Sortable Identifiers):
- Encode timestamp at creation (monotonically increasing)
- `runId`: generated at `run:create` time
- `effectId`: generated at `run:iterate` time when a task is dispatched

## Key files

### `run.json`

```json
{
  "runId": "01KT58MWQKH5XD2H8VFX2W8GTG",
  "processId": "hello",
  "entrypoint": {
    "importPath": "../../pinard/processes/hello.js",
    "exportName": "process"
  },
  "layoutVersion": "2026.01-storage-preview",
  "createdAt": "2026-06-02T22:53:09.763Z",
  "completionProof": "9b8c77ec5776f3438af08079530da055"
}
```

- `processId`: matches `--process` name passed to spawn
- `entrypoint`: path to JS file + exported function name
- `completionProof`: SHA hash the LLM must produce to prove the run finished (stop-hook mechanism)

### `journal/<seq>.<ulid>.json`

The event-sourced truth. On replay, only the journal matters.

| Event type | When | Key data |
|---|---|---|
| `RUN_CREATED` | `run:create` | runId, processId, entrypoint |
| `EFFECT_REQUESTED` | `run:iterate` hits `ctx.task()` | effectId, invocationKey, kind, taskDefRef |
| `EFFECT_RESOLVED` | `task:post` | effectId, status (ok/error), resultRef |
| `RUN_COMPLETED` | process function returns | outputRef |
| `RUN_FAILED` | process function throws | error (name, message, stack) |

Each entry has a `checksum` (SHA-256 of the serialized event) for tamper detection.

### `tasks/<effectId>/task.json`

What the LLM was asked to do:

```json
{
  "kind": "agent",
  "title": "Say hello",
  "agent": {
    "name": "greeter",
    "prompt": { "role": "...", "task": "...", "outputFormat": "..." },
    "outputSchema": { "type": "object", "properties": {...} }
  },
  "inputs": { "name": "world" },
  "effectId": "01KT58N39TP7R05QDXCR7A9C7B",
  "taskId": "greet",
  "invocationKey": "hello:S000001:greet",
  "stepId": "S000001"
}
```

### `tasks/<effectId>/result.json`

What the LLM reported back via `task:post`:

```json
{
  "effectId": "01KT58N39TP7R05QDXCR7A9C7B",
  "status": "ok",
  "result": { "greeting": "Hello, World!" },
  "value": { "greeting": "Hello, World!" },
  "startedAt": "2026-06-02T22:53:20.153Z",
  "finishedAt": "2026-06-02T22:53:20.153Z"
}
```

### `state/state.json`

Derived cache (can be rebuilt from journal). Used for fast lookups:

```json
{
  "effectsByInvocation": {
    "hello:S000001:greet": {
      "effectId": "...",
      "status": "resolved_ok",
      "kind": "agent",
      "requestedAt": "...",
      "resolvedAt": "..."
    }
  },
  "pendingEffectsByKind": {}
}
```

### `state/output.json`

The process function's return value:

```json
{ "greeting": "Hello, World!", "status": "completed" }
```

## Invocation key

Format: `<processId>:<stepId>:<taskId>`

Example: `hello:S000001:greet`

- `processId`: from `run.json`
- `stepId`: monotonic counter (`S000001`, `S000002`, ...) tracking position in the process function
- `taskId`: from `defineTask('greet', ...)` in the process definition

This is what makes replay deterministic: same process code + same journal = same invocation keys = same execution path.

## Replay mechanics

On each `run:iterate`:
1. Load journal events
2. Build an `EffectIndex` (invocationKey → status)
3. Re-run the process function from the top
4. Each `ctx.task()` checks the index:
   - If resolved → return cached result immediately (fast-forward)
   - If requested but not resolved → throw `EffectPendingError` (still waiting)
   - If new → write task definition, append `EFFECT_REQUESTED`, throw `EffectRequestedError`
5. Return pending effects for the harness to execute
