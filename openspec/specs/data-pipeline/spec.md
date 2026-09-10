## Purpose

This specification defines the integration of babysitter (a5c-ai/babysitter) into pinard as the worker-level process orchestrator. It covers both software development workflows (full MR lifecycle as process-as-code) and data pipelines (multi-step, multi-environment, long-running). It establishes the two-loop architecture (conductor loop + babysitter worker loop), process-scoped NATS communication, and deterministic process governance for auditable, replayable operations.

## Background

Pinard's current worker model is freeform: an agent reads an issue or prompt, decides what to do, makes changes, opens an MR. This works for software development tasks where LLM judgment is the primary value. For data pipelines — multi-step, long-running, cross-environment, requiring strict ordering and human validation — the freeform model is insufficient. The process must be explicit, replayable, and auditable.

Babysitter provides process-as-code: JavaScript functions that define the exact sequence of tasks, gates, and parallel groups. The agent can ONLY do what the process code permits. Combined with event-sourced journaling, this enables deterministic replay, crash recovery, and full audit trails.

## Integration Phases

Babysitter integration follows a progressive approach — each phase builds on the previous and can be validated independently.

### Phase 1: Transparent Integration

The babysitter extension SHALL be loaded alongside the worker extension in Pi without changing existing behavior. When no `--process` flag is passed to `aoc spawn`, babysitter remains inert — no run is created, no loop is driven, the worker operates in freeform mode exactly as today.

**Validation:** All existing workers continue to function identically. The babysitter extension loads, detects no process, and does nothing.

### Phase 2: Process Discovery and Loading

The system SHALL support multiple process definitions, discoverable by name. Process definitions live in the project that owns the pipeline (e.g. `example-workers/processes/example-build.js`). Reusable task definitions (`tasks/slurm.js`, `tasks/git-annex-sync.js`) live in a shared location.

`aoc spawn --process <name>` SHALL resolve the process definition by searching `vignes/<project>/processes/` or accepting an absolute path. The resolved path is passed to the babysitter extension as an environment variable.

**Validation:** `aoc spawn --process example-build --project example-workers` loads and starts the correct process definition.

### Phase 3: Software Development Workflow

The current freeform software development workflow SHALL be replaced by a babysitter process definition. This covers the full worker lifecycle: from issue analysis through MR merge, including the review feedback loop.

This is the first production use of babysitter within pinard. It validates the extension coexistence model, the event effect mechanism, and the NATS subject namespacing before data pipelines are introduced.

**Validation:** A worker spawned with `--process dev` completes a full cycle: analyze issue → implement → test → open MR → address reviews → merge. The journal captures every step. A crashed worker resumes from the journal.

### Phase 4: Data Pipeline Processes

New process definitions for data pipelines (starting with Example build on HPC) extend the system to long-running, multi-environment workflows with SLURM jobs, quality gates, and cross-environment handoff over NATS.

**Validation:** The Example build pipeline runs end-to-end on HPC, governed by `example-build.js`, with breakpoints for human validation at each major checkpoint.

## Components

| Component | Role |
|-----------|------|
| **Conductor** | Outer-loop orchestrator. Cross-vigne, cross-environment coordination over NATS. LLM-powered judgment for unexpected situations. |
| **Daemon** | Systemd service. Spawns workers, watches state, triggers processes via schedules or issue labels. |
| **Worker (Pi agent)** | Inner-loop executor. Runs babysitter process within a Pi harness with NATS connectivity. |
| **Babysitter extension** | Pi extension governing process execution: task sequencing, gates, journal, replay. |
| **Worker extension** | Pi extension handling NATS infrastructure: inbox, BTW, interrupt, KV state. |
| **Process definition** | JavaScript file defining the pipeline: tasks, breakpoints, parallel groups, conditional logic. |
| **Task definitions** | Reusable building blocks: SLURM submission, git-annex sync, quality checks, Iceberg operations. |

## Architecture

### Requirement: Two-Loop Model

The system SHALL implement two distinct orchestration loops:

**Outer loop (conductor + NATS):**
- Cross-vigne task assignment
- Cross-environment pipeline coordination (HPC → cloud → EKS)
- Event-driven reaction to worker completions and failures
- LLM-powered judgment for error handling and routing decisions
- NATS JetStream as transport (required for multi-machine coordination)

**Inner loop (babysitter):**
- Single-worker deterministic process execution
- Process-as-code authority (agent cannot deviate)
- Quality gates and human breakpoints
- Event-sourced journal for replay and audit
- Runs within the Pi harness on one machine

#### Scenario: Two-loop coordination
- **GIVEN** a data pipeline spans multiple environments (e.g. HPC build + cloud release)
- **WHEN** the HPC worker's babysitter process completes
- **THEN** the worker SHALL publish a completion event to NATS with the process result
- **AND** the conductor SHALL receive the event and decide the next action (spawn cloud worker, alert human, retry)
- **AND** the cloud worker starts its own babysitter process independently
- **AND** neither worker is aware of the other — coordination is the conductor's responsibility

### Requirement: Worker Runtime Stack

A data pipeline worker SHALL use the following stack:

```
daemon (schedule/issue trigger)
  → aoc spawn --process <process-name>
    → tmux session
      → Pi (harness)
        ├─ worker extension (NATS, KV, BTW, interrupt)
        └─ babysitter extension (process loop, journal, gates)
            └─ agent tasks execute inside babysitter's two-loop model
```

#### Scenario: Worker startup with process
- **WHEN** the daemon runs `aoc spawn --process example-build`
- **THEN** it SHALL create a tmux session with Pi as the harness
- **AND** the worker extension SHALL connect to NATS and subscribe to channels
- **AND** the babysitter extension SHALL load the process definition and start execution
- **AND** the process definition SHALL be the authority for what tasks the agent executes

### Requirement: Pi Agent as Harness

The worker SHALL use Pi (not vanilla Claude Code) as the babysitter harness.

**Rationale:** Pi extensions provide:
- Persistent NATS connection for the session lifetime (Claude Code hooks are stateless)
- Real-time message injection via `sendUserMessage` (inbox, BTW)
- Event-driven lifecycle hooks (`session_start`, `turn_start`, `turn_end`, `message_end`)
- KV state publishing for conductor dashboard visibility
- Interrupt/abort capability via the interrupt channel

Babysitter integrates with Pi via the same hook system it uses for Claude Code (`SessionStart` + `Stop`), since Pi inherits Claude Code's hook interface.

### Requirement: Extension Coexistence

The babysitter extension and worker extension SHALL run as separate Pi extensions within the same session, with clear separation of concerns:

| Concern | Owner |
|---------|-------|
| What tasks to execute, in what order | Babysitter extension |
| Quality gates and breakpoints | Babysitter extension |
| Journal and replay state | Babysitter extension |
| NATS connection and subscriptions | Worker extension |
| KV state publishing | Worker extension |
| BTW and interrupt channels | Worker extension |
| Inbox consumption (initial trigger only) | Worker extension |

#### Scenario: No hook contention
- **GIVEN** both extensions use Pi's hook system
- **THEN** babysitter's `Stop` hook SHALL be the outermost controller (decides when the agent can stop)
- **AND** the worker extension SHALL NOT have its own `Stop` hook — it operates via event listeners (`turn_start`, `turn_end`, `message_end`)
- **AND** the worker extension's `session_start` handler runs first (NATS must connect before babysitter starts)

## Process Definitions

### Requirement: Project-Specific Processes

Process definitions SHALL be project-specific, not generic interpreters.

**Rationale:** Babysitter's value is that the process code IS the authority. A generic loop interpreting a runbook document moves authority back to data interpreted by LLM judgment, negating determinism.

The conditional logic, parallel grouping, and gating decisions SHALL be expressed as JavaScript code, not as configuration parsed at runtime.

#### File structure

```
processes/
  example-build.js          — Example HPC build pipeline (project-specific flow)
  example-release.js        — Example cloud release pipeline (project-specific flow)

tasks/
  slurm.js                 — Generic: submit SLURM job, monitor, interpret result
  git-annex-sync.js        — Generic: sync via git-annex, verify, report
  quality-gate.js           — Generic: run validation command, score output
  iceberg.js               — Generic: build/verify/compact/relocate Iceberg tables
  notify.js                — Generic: publish status to NATS via aoc notify
```

#### Scenario: New pipeline reuses tasks
- **GIVEN** a new data pipeline (e.g. proteomics-db) needs to be defined
- **WHEN** a developer creates `processes/proteomics-build.js`
- **THEN** it SHALL import and compose reusable tasks from `tasks/`
- **AND** the process flow, gates, and conditionals are specific to proteomics
- **AND** no "generic pipeline interpreter" exists — each pipeline is its own process file

### Requirement: Agent Judgment in Tasks

Agent tasks (kind: `agent`) SHALL be used when interpreting results requires contextual judgment beyond exit codes.

**Rationale:** Data pipeline scripts output structured information (study counts, fragment counts, error logs) that requires domain knowledge to interpret. "Zero new studies queued" may be normal (up-to-date release) or abnormal (broken download). An agent task reads script output, applies context from the process inputs and memory, and produces a structured verdict.

Shell tasks (kind: `shell`) SHALL be used for deterministic operations where output interpretation is unnecessary.

#### Scenario: Agent interprets SLURM output
- **WHEN** a SLURM ingestion job completes
- **THEN** the babysitter process SHALL invoke an agent task that:
  - Reads the SLURM logs and exit codes
  - Checks the provenance database for expected vs actual counts
  - Evaluates whether discrepancies are acceptable given the release context
  - Returns a structured result: `{ success: boolean, studiesIngested: number, issues: string[] }`
- **AND** the process definition gates on this structured result, not on the exit code

### Requirement: Breakpoint Semantics

Babysitter breakpoints (`ctx.breakpoint()`) SHALL halt process execution until a human or conductor responds.

#### Scenario: Breakpoint during data pipeline
- **WHEN** the process reaches a `ctx.breakpoint({ question: "..." })`
- **THEN** the babysitter extension SHALL pause the process
- **AND** the worker extension SHALL publish the gate question to `pinard.{vignoble}.agents.{session}.process.{processName}.gate`
- **AND** the conductor or a human SHALL respond via the same NATS subject
- **AND** the babysitter process SHALL resume only after receiving a response
- **AND** the gate interaction SHALL be recorded in the babysitter journal

### Requirement: Event Effect — Deterministic Wait for External Events

Babysitter's effect system SHALL be extended with a new effect kind `event` that allows a process to declare a deterministic wait for an external event. The LLM is NOT involved in the wait — the Pi worker extension resolves the effect when a matching NATS message arrives.

**Rationale:** Many process steps require waiting for external input — pipeline results, review comments, SLURM job completion. These are not agent tasks (no LLM judgment needed to wait), not breakpoints (no human decision needed), and not sleeps (no fixed deadline). They are infrastructure-resolved effects: the process declares what it needs, the extension delivers it.

#### Task definition

```javascript
const waitForEvent = defineTask('wait-for-event', (args) => ({
  kind: 'event',
  title: `Waiting for: ${args.events.join(', ')}`,
  event: {
    types: args.events,       // event types to match
    timeout: args.timeout,    // optional deadline (ms)
  },
}));
```

#### Resolution flow

1. Process calls `ctx.task(waitForEvent, { events: [...] })` — throws `EffectRequestedError`, journals `EFFECT_REQUESTED` with kind `event`
2. `run:iterate` returns `status: "waiting"` with the pending event effect in `schedulerHints`
3. Pi worker extension detects the pending `event` effect — does NOT prompt the LLM
4. Extension subscribes to the process inbox NATS subject and waits
5. When a matching NATS message arrives, the extension calls `babysitter task:post <runDir> <effectId> --status ok --value-inline '<event payload>'`
6. Extension drives the next iteration via `run:iterate`
7. Process replays, finds the resolved event, continues into the appropriate branch

#### Scenario: Event wait during review loop
- **GIVEN** the dev process is in the review loop and calls `ctx.task(waitForEvent, { events: ['pipeline_failed', 'review_comment', 'merged'] })`
- **WHEN** the daemon publishes a `review_comment` event to `pinard.{vignoble}.agents.{session}.process.dev.inbox`
- **THEN** the Pi worker extension SHALL resolve the effect via `task:post` with the event payload
- **AND** the process resumes and dispatches an `addressReview` agent task
- **AND** no LLM tokens are consumed during the wait

#### Scenario: Replay after crash during event wait
- **GIVEN** a process was waiting for an event and the session crashed
- **AND** the NATS event arrived while the session was down (durable consumer)
- **WHEN** the worker is respawned and the process replays from the journal
- **THEN** if the event was already resolved before the crash, replay fast-forwards through it
- **OR** if the event was still pending, the extension re-subscribes and waits for the next message (durable consumer delivers missed messages)

#### Applicability beyond dev workflows

The event effect pattern applies to any infrastructure-resolved wait:

| Use case | Event types | Resolver |
|----------|-------------|----------|
| Dev review loop | `pipeline_failed`, `review_comment`, `merged` | Daemon publishes to process inbox |
| SLURM job wait | `slurm_completed`, `slurm_failed` | Daemon monitors SLURM queue, publishes result |
| Cross-environment handoff | `process_completed` from another worker | Conductor routes between workers |
| Gate response | `gate_approved`, `gate_rejected` | Human or conductor responds |

### Requirement: Process-Scoped NATS Subjects

All babysitter process communication SHALL use process-scoped NATS subjects that include the process name, isolating process traffic from freeform worker traffic and from other processes.

#### Subject hierarchy

The `{agentId}` token is the **run ID** for process-governed workers (stable across respawns) or the **session name** for freeform workers.

```
# Freeform workers (agentId = session name)
pinard.{vignoble}.agents.{agentId}.inbox
pinard.{vignoble}.agents.{agentId}.btw
pinard.{vignoble}.agents.{agentId}.interrupt
pinard.{vignoble}.agents.{agentId}.events.{type}

# Process-governed workers (agentId = run ID, e.g. exo-cli-swe-42)
pinard.{vignoble}.agents.{agentId}.process.{processName}.inbox       ← events FOR the process
pinard.{vignoble}.agents.{agentId}.process.{processName}.gate        ← breakpoint communication
pinard.{vignoble}.agents.{agentId}.process.{processName}.events.{type}  ← process lifecycle events
```

| Channel | Subject | Transport | Persistence | Purpose |
|---------|---------|-----------|-------------|---------|
| Process inbox | `…process.{name}.inbox` | JetStream | Durable | External events delivered to a running process (pipeline_failed, review_comment, merged, slurm_completed) |
| Process gate | `…process.{name}.gate` | JetStream | Durable | Breakpoint questions and responses (must survive conductor restarts) |
| Process events | `…process.{name}.events.{type}` | JetStream | Durable | Process lifecycle events published by the worker (task_started, task_completed, process_completed) |

#### Wildcard examples

```
# All activity for one worker (freeform + process)
pinard.exohub.agents.worker-001.>

# All example-build activity across all sessions and vignobles
pinard.*.agents.*.process.example-build.>

# All process gates across a vignoble
pinard.exohub.agents.*.process.*.gate

# All process lifecycle events across all vignobles
pinard.*.agents.*.process.*.events.>
```

#### Scenario: Daemon routes to process inbox
- **GIVEN** a worker was spawned with `aoc spawn --process swe --project exo-cli --issue 42`
- **AND** the run ID is `exo-cli-swe-42` (deterministic from project-process-issue)
- **WHEN** the daemon detects a pipeline failure on the worker's MR
- **THEN** the daemon SHALL publish the event to `pinard.exohub.agents.exo-cli-swe-42.process.swe.inbox`
- **AND** the Pi worker extension SHALL resolve the pending `waitForEvent` effect with the event payload
- **AND** a respawned worker subscribes to the same subject (run ID is stable)

#### Scenario: Freeform workers unaffected
- **GIVEN** a worker was spawned without `--process` (freeform mode)
- **THEN** the daemon SHALL publish events to `pinard.{vignoble}.agents.{sessionName}.inbox` as today
- **AND** no `.process.` subjects are created or consumed

#### Stream configuration

The existing stream wildcard `pinard.*.agents.*.events.>` captures freeform worker events. A new stream SHALL capture all process traffic:

```
Stream: PINARD_PROCESSES
Subjects: pinard.*.agents.*.process.>
```

This captures process inboxes, gates, and lifecycle events in a single stream, enabling cross-vignoble monitoring and replay.

## Multi-Environment Coordination

### Requirement: Cross-Environment Pipeline

A data pipeline that spans multiple compute environments SHALL be orchestrated as separate babysitter processes coordinated by the conductor over NATS.

#### Scenario: HPC build → cloud release
- **GIVEN** the Example pipeline has two phases: HPC build (steps 1-12) and cloud release (S3 → EKS/FSx)
- **AND** each phase runs on a different machine with a different pinard instance
- **WHEN** the HPC worker's babysitter process completes successfully
- **THEN** the process SHALL return a result object describing what was built
- **AND** the worker extension SHALL publish a `process_completed` event to NATS with the result
- **AND** the conductor (which may be on either machine, or a third) SHALL receive the event
- **AND** the conductor SHALL spawn a cloud worker with `aoc spawn --process example-release --args <result>`
- **AND** the cloud worker's babysitter process SHALL receive the HPC result as `inputs`

#### Scenario: Cross-environment failure
- **WHEN** the cloud release process fails (e.g. FSx sync timeout)
- **THEN** the cloud worker SHALL publish a `process_failed` event to NATS
- **AND** the conductor SHALL decide: retry the cloud phase, alert a human, or roll back
- **AND** the HPC worker's completed process is NOT re-executed (its journal is final)

### Requirement: NATS as Cross-Environment Transport

NATS JetStream SHALL be the sole transport for cross-environment coordination.

**Rationale:** Babysitter is single-machine — it has no concept of distributed orchestration. NATS provides the guaranteed delivery, persistence, and multi-datacenter connectivity required for HPC ↔ cloud communication. This is pinard's unique value over babysitter alone.

## LLM Reliability and Effect Lifecycle

### Background: Babysitter's Enforcement Model

Babysitter's orchestration loop relies on the LLM performing two critical actions each iteration:

1. **`run:iterate`** — called at the start of each turn to get the next pending task from the journal
2. **`task:post`** — called after completing work to record the result in the journal

The enforcement mechanisms are:

| Mechanism | What it prevents | How |
|-----------|-----------------|-----|
| Stop hook (block decision) | LLM finishing without iterating | Blocks exit, injects "Continue orchestration (run:iterate)" as context |
| Completion proof (`<promise>` tag) | LLM claiming it's done prematurely | Must output SHA-256 hash from run status — can't be guessed |
| Max iterations guard | Infinite loops | Allows exit after 256 iterations (configurable) |
| Speed guard | Runaway tight loops | Allows exit if avg iteration < 15s |

**The `run:iterate` side is mechanically robust.** If the LLM doesn't call it, the stop hook blocks, injects a reminder, and the LLM gets another chance. This repeats reliably.

**The `task:post` side is NOT mechanically enforced.** After the LLM performs a long task (running SLURM jobs, interpreting logs, checking databases), it must remember to call `task:post` with the correct `effectId` and result. If it forgets:
- The work is done but not journaled
- Next iteration re-requests the same task
- Work is duplicated (safe if idempotent, but wasteful)

This is a **prompt-only guarantee** — babysitter relies on system prompt instructions ("CRITICAL RULE: post your result") and the stop hook's injected context to remind the LLM. There is no mechanical fallback.

### Requirement: Pi Extension as Effect Lifecycle Safety Net

The Pi worker extension SHALL monitor the babysitter effect lifecycle and compensate for LLM forgetfulness on `task:post`.

**Rationale:** For data pipelines with SLURM jobs that take hours, re-executing a task because the LLM forgot to post its result is unacceptable (even if idempotent). Pi's persistent extension model (event-driven, stateful across turns) can observe what the LLM did vs what it should have done — something babysitter's Claude Code adapter (stateless shell hook) cannot do.

#### Scenario: LLM completes work but forgets task:post
- **GIVEN** the babysitter process requested an agent task (effectId X)
- **AND** the LLM performed the work (ran scripts, produced output)
- **AND** the LLM's turn ended without calling `task:post`
- **WHEN** the Pi worker extension's `turn_end` hook fires
- **THEN** the extension SHALL check for pending effects whose work appears completed:
  - For `shell` tasks: check if the command's output files exist and exit code was captured
  - For `agent` tasks: check if the task's expected output artifacts exist on disk
- **AND** if evidence of completion is found, the extension SHALL auto-post the result via `babysitter task:post`
- **AND** the extension SHALL log a warning: "auto-posted result for effectId X — LLM forgot task:post"
- **AND** the journal SHALL record the result as if the LLM had posted it normally

#### Scenario: LLM loses track of effectId in long context
- **GIVEN** the LLM has consumed many tokens since receiving the effectId from `run:iterate`
- **AND** the Pi extension recorded the current effectId at task dispatch time (from `turn_start` or by observing the `run:iterate` output)
- **WHEN** the LLM's turn ends
- **THEN** the extension SHALL have the effectId available from its own state (not dependent on the LLM remembering it)

### Requirement: Effect State Tracking in Worker Extension

The Pi worker extension SHALL maintain a local effect tracker:

```typescript
interface EffectTracker {
  currentEffectId: string | null;
  currentTaskKind: 'agent' | 'shell' | 'breakpoint' | 'sleep' | 'event';
  dispatchedAt: string;  // ISO timestamp
  expectedOutputPath: string | null;  // from task definition io.outputJsonPath
}
```

This tracker is populated when the extension observes `run:iterate` output (by intercepting the CLI call or reading the journal after dispatch), and cleared when `task:post` is observed.

#### Scenario: Detecting missed task:post at turn boundary
- **GIVEN** `effectTracker.currentEffectId` is non-null at `turn_end`
- **AND** no `task:post` call was observed during this turn
- **THEN** the extension SHALL attempt auto-recovery (scenario above)
- **OR** if recovery is not possible (no evidence of completion), publish a warning event to NATS: `process_task_post_missed`
- **AND** the conductor SHALL surface this as an anomaly in the dashboard

### Requirement: Additional Pi Enforcement for Data Pipelines

Beyond `task:post` safety, the Pi extension SHALL enforce:

| Concern | Pi extension behavior |
|---------|----------------------|
| SLURM job submitted but session about to terminate | Publish `process_slurm_pending` event to NATS with job ID, so daemon can monitor independently |
| Agent task running > N hours without progress | Publish `process_task_stalled` event; conductor can decide to interrupt or extend |
| Breakpoint reached but no gate response within timeout | Re-publish gate request to NATS (in case original was lost) |
| Process completed but no `process_completed` NATS event published | Auto-publish on `session_end` if journal shows RUN_COMPLETED |

## Quality Assurance in Process Definitions

### Background: Babysitter Does Not Validate Quality

Babysitter's SDK determines "completeness" purely from journal state (all effects resolved → process function returns → `RUN_COMPLETED`). It does NOT validate:
- Whether posted results are truthful
- Whether work was actually performed
- Whether output matches the declared `outputSchema`
- Whether quality criteria are met

The LLM can post `{status: "ok", value: {ingested: 5000, failures: 0}}` without running the ingestion script, and babysitter will accept it, advance the process, and eventually emit a completion proof.

Quality assurance is entirely the **process author's responsibility** — enforced through the task graph design, not the SDK.

### Requirement: Verification Task Pattern

Data pipeline process definitions SHALL include independent verification tasks after every critical step. The executor task and the verification task SHALL be separate agent invocations with different prompt angles.

**Rationale:** A single agent self-reporting success is an unverified claim. A separate verification task with a skeptical prompt ("find problems, default to rejection if uncertain") creates a trust chain where fabrication requires two independent LLMs to collude.

#### Scenario: Shell-based objective verification
- **GIVEN** an ingestion task claims to have processed N studies
- **THEN** the process SHALL include a `shell` task that independently counts rows:
  ```javascript
  const verify = await ctx.task(countIngested, { shardPath });
  // shell task: python -c "import tiledb; print(len(tiledb.open('...').meta))"
  ```
- **AND** the process code SHALL gate on the shell result (which the LLM cannot fabricate — exit code and stdout are captured directly)

#### Scenario: Agent-based adversarial verification
- **GIVEN** a task interprets SLURM logs and reports success
- **THEN** the process MAY include a second agent task with an adversarial prompt:
  - "Review the SLURM logs independently. Assume the previous report may be wrong. Look for hidden failures, silent data loss, or suspiciously identical output to previous builds."
- **AND** the process SHALL gate on the verifier's structured judgment

### Requirement: Shell Tasks for Objective Checks

Where objective, measurable verification is possible (file existence, row counts, checksums, exit codes), the process SHALL prefer `shell` tasks over `agent` tasks for verification.

**Rationale:** Shell task results are mechanically captured (stdout, stderr, exit code). The LLM executes the command but cannot fabricate the output of a command it actually ran. This provides a ground-truth signal the process code can gate on.

```javascript
// Objective: shell task captures real filesystem state
const fileCount = await ctx.task(checkParquetFiles, {});
// shell: find $PARQUET_DIR -name "*.parquet" | wc -l

// Subjective: agent interprets whether count is acceptable
const judgment = await ctx.task(evaluateCount, {
  actual: fileCount.stdout.trim(),
  expected: studies.count,
  context: inputs.releaseType
});

// Deterministic gate in process code
if (!judgment.acceptable) {
  await ctx.breakpoint({ question: `Count mismatch: ${judgment.reason}. Override?` });
}
```

### Requirement: Breakpoints as Human Quality Oracle

For critical decisions where automated verification is insufficient, `ctx.breakpoint()` SHALL be used to insert human judgment. The breakpoint question SHALL include enough context for the human to make an informed decision without re-reading logs.

**Rationale:** Babysitter cannot mechanically verify "is this release correct?" — that requires domain knowledge. Breakpoints are the only quality gate that cannot be bypassed by a lying LLM (assuming the human actually reviews).

### Why This Favors Pi Over Vanilla Claude Code

| Capability needed | Claude Code hooks | Pi extensions |
|---|---|---|
| Observe LLM tool calls within a turn | No (hook fires only at turn boundary) | Yes (`message_end`, tool call events) |
| Maintain state across turns | No (stateless shell invocation) | Yes (long-lived process with variables) |
| Auto-post results on LLM forgetfulness | No (can only block/allow exit) | Yes (can call CLI from extension code) |
| Publish NATS events on anomalies | No (no NATS connection) | Yes (persistent connection) |
| Track effectId independently of LLM context | No | Yes (extension reads iterate output) |

This is a primary architectural justification for keeping Pi as the harness rather than using vanilla Claude Code with babysitter.

## Long-Running Execution

### Requirement: Multi-Day Process Support

Data pipeline processes that run for days or weeks SHALL be supported through session respawn with contextual memory.

#### Scenario: Worker respawn on long pipeline
- **GIVEN** a babysitter process includes SLURM jobs that run for days
- **AND** the worker's Claude session may be terminated (timeout, OOM, machine restart)
- **WHEN** the worker session ends unexpectedly
- **THEN** the daemon SHALL detect the session loss (KV state disappears or `session_ended` event)
- **AND** the daemon SHALL respawn the worker with the same process and run ID
- **AND** the babysitter extension SHALL resume from the journal (deterministic replay of completed tasks)
- **AND** the worker's contextual memory (Graphiti/Zep) SHALL provide domain context that the journal cannot capture
- **AND** the first un-journaled task resumes live execution

### Requirement: Idempotent Tasks

All tasks in a data pipeline process SHALL be idempotent or explicitly guarded against re-execution.

**Rationale:** Journal replay may re-invoke tasks that partially completed before a crash. The underlying scripts (e.g. Example's provenance DB) must handle re-execution safely.

#### Scenario: Replay after crash during SLURM job
- **GIVEN** the process submitted a SLURM ingestion job and crashed before recording the result
- **WHEN** the process is resumed from the journal
- **THEN** the replay SHALL re-execute the ingestion task
- **AND** the ingestion script SHALL detect already-ingested studies via the provenance DB and skip them
- **AND** the task result reflects the combined outcome (previously ingested + newly ingested)

## Event Integration

### Requirement: Journal → NATS Bridge

Babysitter journal events SHALL be bridged to NATS for conductor visibility.

| Babysitter journal event | NATS event |
|--------------------------|------------|
| Task started | `process_task_started` |
| Task completed | `process_task_completed` |
| Task failed | `process_task_failed` |
| Breakpoint reached | `process_gate_pending` |
| Breakpoint resolved | `process_gate_resolved` |
| Process completed | `process_completed` |
| Process failed | `process_failed` |

These events SHALL be published to `pinard.{vignoble}.agents.{session}.process.{processName}.events.{type}` using process-scoped NATS subjects.

### Requirement: Spawn with Process

The `aoc spawn` command SHALL accept `--process`, `--parcelle`, `--issue`, and `--run-id` flags:

```bash
aoc spawn --process example-build \
          --project example-workers \
          --issue 42 \
          --parcelle data-pipeline \
          --args '{"source": "ebi", "threshold": 85}'
```

- `--process` identifies the process definition (resolved from vignes/<project>/processes/ or pinard/processes/)
- `--parcelle` selects the workstream (defaults to project name)
- `--issue` ties the work to a GitLab issue (enables deterministic run ID)
- `--run-id` explicitly resumes an existing run
- `--args` provides the process inputs (available as `inputs` in the process function)
- `--prompt` is optional when `--process` is set

#### Per-vigne default process

Each vigne MAY declare a default process in `vignes.yaml`:

```yaml
vignes:
  exo-cli:
    process: swe
  example-workers:
    process: example-build
```

The conductor's `spawn_agent` tool, `/spawn` command, and daemon issue watcher all read the vigne's process from config. Falls back to `swe` when not specified.

#### Issue-driven spawn

When an issue is assigned to the pinard user, the daemon spawns a worker automatically:
- Spawn uses the vigne's configured process
- Labels `blocked` or `pinard:discarded` prevent spawn
- The issue IID produces a deterministic run ID (resumable on crash)
- On MR close without merge: worker adds `pinard:discarded` label and unassigns

### Requirement: Status Visibility

`aoc status` SHALL display process state for active workers:

```
Workers:
  example-build-001  example-workers  process:example-build  step:5/12 (optimize_shard)  gate:none  ● NATS
  example-rel-001    example-api      process:example-release step:2/4 (sync_fsx)         gate:pending  ● NATS
```

## Reference Implementation: Example Build

The Example build runbook (`example-workers/docs/build-runbook.md`) SHALL be the first babysitter process implementation. It demonstrates:

- 12 sequential steps with parallel opportunities (steps 5/6/7)
- Human validation gates at each major checkpoint
- SLURM long-running jobs (hours to days)
- Conditional logic (new shard decision based on study count)
- Cross-environment handoff (HPC → S3 → cloud)
- Agent judgment for interpreting results (study counts, fragment counts, SLURM logs)
- Idempotent scripts via provenance database

### Process outline

```javascript
async function process(inputs, ctx) {
  // Phase 1: Acquisition (sequential, each depends on previous)
  const downloadList = await ctx.task(prepareDownloadList, { source: inputs.source });
  await ctx.breakpoint({ question: `${downloadList.newRows} new studies. Proceed?` });

  const downloads = await ctx.task(downloadGwas, { batchSize: inputs.batchSize || 1000 });
  await ctx.breakpoint({ question: `${downloads.remaining} remaining. Spot-check passed?` });

  const studies = await ctx.task(discoverStudies);

  // Conditional: new shard decision
  if (studies.currentShardCount >= inputs.studiesPerShard || 30000) {
    await ctx.breakpoint({ question: 'Shard at capacity. Confirm new shard?' });
  }

  await ctx.task(ingestStudies, { studyList: studies.outputFile });
  await ctx.breakpoint({ question: `Ingestion complete. ${studies.failed} failures. Proceed?` });

  // Phase 2: Optimization + mappings (parallel where possible)
  const [optimized, mappings, metadata] = await ctx.parallel.all([
    () => ctx.task(optimizeShard, { shardPath: inputs.shardPath }),
    () => ctx.task(buildMappings, { studyList: studies.outputFile }),
    () => ctx.task(buildStudyMetadata),
  ]);
  await ctx.breakpoint({
    question: `Shard: ${optimized.fragments} fragments. Mappings: ${mappings.dataPoints} points. Index: ${metadata.docCount} docs. Proceed to S3?`
  });

  // Phase 3: Export (sequential, S3 before Iceberg)
  await ctx.task(syncToS3);
  await ctx.task(buildIceberg, { incremental: true });
  const stats = await ctx.task(verifyIceberg);
  await ctx.task(compactIceberg, { retainLast: 1 });
  await ctx.task(relocateIceberg, {
    oldPrefix: inputs.buildPath,
    newPrefix: inputs.servePath,
  });

  return {
    status: 'ready_for_release',
    studyCount: studies.count,
    icebergStats: stats,
  };
}
```

## Software Development Process

### Requirement: Full-Lifecycle Dev Workflow

The software development workflow SHALL be expressed as a babysitter process covering the entire worker lifecycle — from issue analysis through MR merge. The process ends only when the MR is merged (or abandoned), not at `track_mr`.

**Rationale:** Today the workflow is split: the LLM handles the "create" phase freeform, the daemon handles post-MR events (pipeline failures, review comments) by dispatching to the worker inbox, and the LLM handles those ad-hoc. This split means the review cycle is un-journaled, non-replayable, and relies on LLM judgment for flow decisions that should be deterministic. A full-lifecycle process captures the entire audit trail and lowers conductor-worker coordination overhead — the worker is self-contained until merge.

#### Process outline

```javascript
async function process(inputs, ctx) {
  // Phase 1: Understand
  const analysis = await ctx.task(analyzeWork, {
    issue: inputs.issue,
    prompt: inputs.prompt,
  });

  // Phase 2: Implement
  await ctx.task(implement, { plan: analysis.plan });

  // Phase 3: Verify (conditional on change type)
  if (analysis.changeType !== 'docs') {
    let tests = await ctx.task(runTests, {});
    let attempts = 0;
    while (!tests.passing && attempts < 3) {
      await ctx.task(fixTests, { failures: tests.failures });
      tests = await ctx.task(runTests, {});
      attempts++;
    }
    if (!tests.passing) {
      await ctx.breakpoint({
        question: `Tests failing after ${attempts} attempts. Proceed anyway?`,
      });
    }
  }

  // Phase 4: Open MR
  const mr = await ctx.task(openMR, {
    targetBranch: inputs.targetBranch,
    issueId: inputs.issueId,
  });
  await ctx.task(trackMR, { mrIid: mr.iid });

  // Phase 5: Review loop — deterministic wait for external events
  let merged = false;
  while (!merged) {
    const event = await ctx.task(waitForEvent, {
      events: ['pipeline_failed', 'review_comment', 'merged'],
    });
    if (event.type === 'pipeline_failed') {
      await ctx.task(fixPipeline, { pipelineUrl: event.pipelineUrl });
    } else if (event.type === 'review_comment') {
      await ctx.task(addressReview, { comments: event.comments });
    } else if (event.type === 'merged') {
      merged = true;
    }
  }

  return { mrIid: mr.iid, status: 'merged' };
}
```

#### Scenario: Docs-only change skips tests
- **GIVEN** a worker is spawned with `--process dev` for a documentation issue
- **WHEN** the `analyzeWork` task returns `changeType: "docs"`
- **THEN** the process SHALL skip the test phase entirely (deterministic conditional in JavaScript)
- **AND** proceed directly to opening the MR

#### Scenario: Review feedback cycle
- **GIVEN** the MR is open and tracked
- **WHEN** the daemon dispatches a `review_comment` event
- **THEN** the Pi worker extension SHALL resolve the pending `waitForEvent` effect with the event payload
- **AND** the process SHALL invoke an `addressReview` agent task to read and respond to the comments
- **AND** the process SHALL loop back to `waitForEvent` for the next event
- **AND** every review cycle is journaled — replayable after a crash

#### Scenario: Per-project process variants
- **GIVEN** `exohub-website` has no test suite
- **THEN** it SHALL have its own process definition (`vignes/exohub-website/processes/dev.js`) with no test phase
- **AND** the default dev process (`processes/dev.js`) SHALL be used for projects without a per-vigne override

### Requirement: Daemon Role Simplification

With the full-lifecycle process, the daemon's role for process-governed workers reduces to:

| Concern | Daemon responsibility |
|---------|----------------------|
| Spawn | `aoc spawn --process dev` (unchanged) |
| Event delivery | Publish events to `...process.{processName}.inbox` (not freeform `.inbox`) |
| Session health | Detect session death, respawn with same process + run ID for journal replay |
| Auto-merge | Unchanged — merge when CI passes + approved |
| Cleanup | Kill tmux + remove KV after process completes or session ends |

The daemon no longer needs to decide what to dispatch or how the worker should handle events — the process definition governs that.

## Open Questions

1. **Babysitter as Pi extension packaging** — How should the babysitter SDK be packaged as a Pi extension? Does it need a custom `HarnessAdapter` implementation for Pi, or can it reuse the Claude Code adapter since Pi inherits the same hook system?

2. **Gate channel consumer** — Who consumes gate responses: the conductor (LLM decides), the daemon (automated policy), or a dedicated human UI? Initial implementation should support conductor consumption with human override via the `…process.{name}.gate` subject.

3. **Process definition discovery** — Process definitions live in the project repo (`vignes/<project>/processes/`) with per-vigne overrides. Reusable task definitions may live in a shared package. Exact resolution logic for `aoc spawn --process <name>` TBD.

4. **Contextual memory integration** — How does Graphiti/Zep memory interact with babysitter's journal on respawn? The journal provides process state; memory provides domain context. What's the handoff protocol?

5. **SLURM sleep strategy** — Should long SLURM waits use the `event` effect (daemon monitors SLURM queue, publishes `slurm_completed` to process inbox) or should the session stay alive with `ctx.sleepUntil()`? The event effect approach allows session termination and respawn, reducing resource usage for multi-day jobs.

6. **Effect tracker implementation depth** — How deeply should the Pi extension introspect babysitter's journal to detect missed `task:post` calls? Options: (a) lightweight — check if expected output file exists on disk, (b) medium — read the journal and compare against observed CLI calls, (c) deep — parse the LLM's transcript to extract results and auto-post. Tradeoff: deeper introspection catches more failures but couples Pi tightly to babysitter's internals.

7. **Upstream contribution** — Should the effect lifecycle safety net and the `event` effect kind be proposed upstream to babysitter, or kept as pinard-specific extensions? If upstream, babysitter's `HarnessAdapter` interface may need `onTurnEnd(pendingEffects)` and the SDK would need to support infrastructure-resolved effects natively.
