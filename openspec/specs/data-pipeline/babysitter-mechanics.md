# Babysitter Internals: How Deterministic Code and LLM Execution Actually Work

This document captures the deep-dive analysis of babysitter's architecture (from the `a5c-ai/babysitter` source code), how it maps to pinard's data pipeline use case, and the reliability concerns identified during the integration design.

## The Core Mechanism: Effects + Exceptions

Babysitter doesn't call the LLM directly. It uses a **file-based effect system** where the deterministic process and the LLM communicate through disk:

1. **Deterministic code** (process.js) runs, hits `ctx.task()`, throws an exception
2. **Task definition** (JSON) is written to `.a5c/runs/<runId>/tasks/<effectId>/task.json`
3. **LLM** reads the task definition, performs the work, writes result to disk
4. **Stop hook** prevents the LLM from finishing, forces next iteration
5. **Deterministic code** replays from journal, finds result, continues to next task

The process never "calls" the LLM. It declares what needs to happen. The harness (Claude/Pi) is the executor. The journal makes it all replayable.

## The Two Loops in Practice

```
HARNESS (Claude Code / Pi)

  Claude's turn starts
    |
  Claude calls: babysitter run:iterate
    |
  +----------------------------------------------------------+
  |  ORCHESTRATION LOOP (deterministic, in-process)          |
  |                                                          |
  |  1. Load journal from .a5c/runs/<runId>/                 |
  |  2. Replay completed effects (fast-forward)              |
  |  3. Execute process function until next un-resolved task |
  |  4. Task intrinsic THROWS EffectRequestedError           |
  |  5. Return {status: "waiting", nextActions: [...]}       |
  +----------------------------------------------------------+
    |
  Claude reads nextActions -> sees task definition
  Claude EXECUTES the task (writes code, runs commands, etc.)
  Claude calls: babysitter task:post --effect-id X --result {...}
    |
  Result written to .a5c/runs/<runId>/tasks/<effectId>/result.json
    |
  Claude's turn ends -> STOP HOOK fires
    |
  Stop hook checks: run completed? -> NO (still pending effects)
  Stop hook outputs: {decision: "block", reason: "continue loop"}
    |
  Claude's turn is BLOCKED from ending
  Claude receives the "reason" as context for next iteration
  Claude calls: babysitter run:iterate again
    |
  Process replays, finds result for previous task, continues...
  Next ctx.task() -> throws again -> next action -> cycle repeats

  UNTIL: process function returns -> RUN_COMPLETED -> stop hook
  sees completion proof -> allows Claude to stop
```

## The Critical Mechanism: `throw EffectRequestedError`

When `ctx.task(someTask, args)` is called in the process, it doesn't execute anything. Looking at `packages/sdk/src/runtime/intrinsics/task.ts`:

1. It generates a `stepId` and computes an `invocationKey` (hash of processId + stepId + taskId)
2. Checks the `effectIndex` — if a result already exists (from a previous iteration), returns it immediately
3. If no result exists: serializes the task definition to disk, appends `EFFECT_REQUESTED` to the journal, and **throws `EffectRequestedError`**

The `orchestrateIteration` function (in `runtime/orchestrateIteration.ts`) catches this:

```typescript
const waiting = asWaitingResult(error);
if (waiting) {
  return { status: "waiting", nextActions: waiting.nextActions };
}
```

This means **the process function runs MANY times** (once per iteration). Each time, it replays completed tasks instantly from the journal and halts at the first un-resolved task.

## Task Kinds and Who Executes Them

| Kind | Who executes | How |
|------|-------------|-----|
| `agent` | Claude (the LLM) | Claude reads the agent prompt from the task definition, performs the work using tools (write files, run commands, read code), then calls `task:post` with a structured result |
| `shell` | Claude (or a dispatcher) | Claude runs the shell command, captures output, posts result |
| `breakpoint` | Human (external) | The stop hook detects only breakpoints pending, allows exit. Human must approve via `babysitter task:post --approve` |
| `sleep` | Timer/scheduler | Process pauses until a target timestamp |

## The Stop Hook Decision Tree

From `packages/sdk/src/harness/claudeCode.ts`:

```
Claude finishes a turn -> stop hook fires
  +-- No session file? -> ALLOW exit (no babysitter loop active)
  +-- Max iterations reached (default 256)? -> ALLOW exit
  +-- Iteration too fast (avg < 15s over last 3)? -> ALLOW exit (runaway detection)
  +-- No run bound to session? -> ALLOW exit
  +-- Run completed + completion proof matches <promise> tag? -> ALLOW exit
  +-- Run waiting on ONLY breakpoints? -> ALLOW exit (needs human, spinning is useless)
  +-- Otherwise? -> BLOCK exit, inject iteration context as "reason"
```

The "reason" injected on block includes:
- The iteration number and max
- The run state (waiting, failed, created)
- Pending effect kinds
- The original user prompt (optionally compressed for long contexts)
- Discovered skills/agents available in the plugin

## Journal and Replay (the determinism source)

The journal is a sequence of JSON files in `.a5c/runs/<runId>/journal/`:

```
001-RUN_CREATED.json
002-EFFECT_REQUESTED.json  (task A requested)
003-EFFECT_RESOLVED.json   (task A result posted)
004-EFFECT_REQUESTED.json  (task B requested)
005-EFFECT_RESOLVED.json   (task B result posted)
...
```

On each `run:iterate`, the `createReplayEngine` function:
1. Loads all journal events
2. Builds an `EffectIndex` mapping invocation keys to their status (requested/resolved/error)
3. Creates a `ReplayCursor` that tracks step progression
4. Re-runs the process function — `ctx.task()` checks the index, returns cached results or throws

This is deterministic: same journal + same process code = same execution path.

## The Completion Proof Mechanism

The LLM cannot simply say "I'm done" and exit. When a run completes (process function returns), the journal records `RUN_COMPLETED` with a completion proof (SHA-256 hash). The stop hook checks:

1. Does the LLM's output contain `<promise>SECRET</promise>`?
2. Does the SECRET match the run's `completionProof`?
3. Only if both: allow exit

The LLM gets the proof from `run:status --json` output. This prevents the LLM from escaping the loop by claiming completion prematurely.

## Completeness vs Quality: What Babysitter Actually Checks

This is a critical distinction: **completeness is deterministic, quality is not checked at all.**

### "Is the run complete?" — Pure Journal State (Deterministic)

The stop hook and `run:iterate` don't evaluate quality. They check the journal:

```
Has journal event RUN_COMPLETED?
  → yes: run is done, emit completionProof, allow exit
Has journal event RUN_FAILED?
  → yes: run failed, inject "Run failed. Fix and proceed."
Are there EFFECT_REQUESTED without matching EFFECT_RESOLVED?
  → yes: still waiting, block exit, inject "Waiting on: <kinds>"
None of the above?
  → run is in "created" state, inject "Continue orchestration"
```

`RUN_COMPLETED` is only appended when the `process()` function **returns normally** — meaning every `ctx.task()` resolved and every `ctx.breakpoint()` was approved. It's purely structural.

### "Did the LLM do a good job?" — Nobody Checks

When `task:post` is called, `commitEffectResult` (in `runtime/commitEffectResult.ts`) validates ONLY structural correctness:

| What it checks | How |
|---|---|
| Does effectId exist in journal? | Lookup in EffectIndex |
| Is the effect still in `requested` status? | Rejects double-posting |
| Does invocationKey match? | Prevents cross-wiring |
| Is payload well-formed? | Has `value` for ok, `error` for error, types correct |

What it does **NOT** check:

| Not checked | Consequence |
|---|---|
| Whether the result is truthful | LLM can lie about what it did |
| Whether work was actually performed | Can post `{score: 100}` without running tests |
| Whether output matches the task's `outputSchema` | Schema is advisory for the LLM prompt, not validated on post |
| Whether quality criteria are met | No quality gate at the SDK level |

The LLM can post `{status: "ok", value: {ingested: 5000, failures: 0}}` without ever running the ingestion script, and babysitter will accept it, append `EFFECT_RESOLVED`, and advance the process.

### Quality Gates Are the Process Author's Responsibility

Babysitter's position: the **process definition** is where quality enforcement lives, not the SDK. The pattern:

```javascript
// Task 1: Do the work (LLM self-reports)
const result = await ctx.task(ingestStudies, { studyList });

// Task 2: Independent verification (different LLM call, skeptical prompt)
const verification = await ctx.task(verifyIngestion, {
  expected: studies.count,
  actual: result.ingested,
  releaseContext: inputs.releaseType
});

// Deterministic gate (JavaScript, not LLM)
if (!verification.acceptable) {
  throw new Error(`Verification failed: ${verification.reason}`);
}
```

The key elements:

1. **Separation of executor and verifier** — The task that does the work is NOT the same task that judges quality. Two separate agent tasks with different prompts.
2. **Deterministic gating on structured output** — The `if (!verification.acceptable)` is JavaScript. The LLM can't bypass it. If the verifier says "not acceptable," the process fails or loops.
3. **Convergence loops** — `while (quality < target) { refine(); score(); }` is code. The LLM executes within the loop but can't escape it.

### The Trust Chain

```
Process code (deterministic) ←── trusts ──→ LLM-posted results (not verified)
      |                                              |
      |  decides what to do next                     |  self-reported by agent
      |  gates on structured values                  |  could be fabricated
      |  can't be bypassed                           |  could be wrong
      |                                              |
      └── mitigation: separate verifier task ────────┘
          (different prompt, skeptical angle)
```

Two LLMs colluding (executor lies AND verifier approves the lie) is less likely than one lying, especially with adversarial prompting on the verifier ("your job is to find problems, default to rejection if uncertain").

But ultimately, **babysitter trusts the LLM's posted results**. The "determinism" is in flow control (what runs next, whether to loop, when to gate), not in result validation.

### Implications for Data Pipelines

For Example, this means:

- The process should include **verification tasks** after every major step (not just trust the executor)
- Verification tasks can use `kind: 'shell'` for objective checks (file existence, row counts, exit codes) that the LLM can't fake
- Agent verification tasks should have adversarial prompts ("check if the output is suspiciously identical to the last build")
- The `ctx.breakpoint()` gates add human judgment at critical points — the human IS the quality oracle that babysitter can't provide mechanically

The process author's job is to design a task graph where lying is structurally difficult: shell tasks verify file system state, a separate agent interprets results skeptically, and breakpoints let humans override.

## Prompt Engineering for Enforcement

Babysitter injects extensive prompting (from `packages/sdk/src/prompts/templates/`):

**Critical rules** (`critical-rules.md`):
- "CRITICAL RULE: After run:create or any posted effect result, end the current turn and yield back to the hook loop. The stop-hook drives the loop, not you."
- "CRITICAL RULE: NEVER bypass the babysitter orchestration model. Do not execute tasks yourself, do not create helper scripts."

**Effects** (`effects.md`):
- "After execution, post the outcome summary into the run by calling `task:post`"
- "Make sure the change was actually performed and not described or implied"

**Loop control** (`loop-control.md`):
- "Do not run multiple `run:iterate` steps in the same turn"
- "Common mistakes to avoid: calling run:iterate multiple times in the same session without stopping"

**Results posting** (`results-posting.md`):
- Detailed instructions on `task:post` syntax, flags, and common mistakes
- "Do NOT write `result.json` directly. The SDK owns that file."

## The Reliability Gap: `task:post` Forgetfulness

The `run:iterate` call is mechanically robust — the stop hook keeps reminding the LLM until it calls iterate. But `task:post` is the vulnerability:

| Failure mode | What happens | Mitigation |
|---|---|---|
| LLM forgets `run:iterate` | Stop hook blocks, injects reminder, LLM tries again next turn | **Self-healing** via stop hook |
| LLM forgets `task:post` | Work done but not journaled; next iterate re-requests same task | **Prompt-only** — no mechanical fallback. Idempotency saves correctness but wastes tokens/time |
| LLM posts wrong result | Journal records bad state, process diverges | **None** — pure LLM trust |
| LLM invents result without doing work | Process advances on false data | **None** — "non-negotiables" prompt says don't fake |
| LLM loses effectId in long context | `task:post` with wrong ID fails or misroutes | Babysitter rejects mismatched IDs, but LLM may not recover gracefully |

For data pipelines with multi-hour SLURM jobs, re-executing because the LLM forgot `task:post` is unacceptable. This is why the spec requires Pi's worker extension to act as a safety net — tracking the current effectId independently and auto-posting results at turn boundaries when the LLM misses.

## Babysitter's Pi Harness Adapter

From `packages/sdk/src/harness/pi.ts`: babysitter already has a Pi adapter. Key points:

- Detects Pi via `BABYSITTER_SESSION_ID`, `PI_SESSION_ID`, or `PI_PLUGIN_ROOT` env vars
- Session binding writes state to a markdown file with YAML frontmatter (same format as Claude Code)
- The stop hook handler is a **no-op** (`writeNoopHookResult`) — Pi's extension model replaces stop-hook-driven orchestration with loop-driver mode (the extension drives the loop via `agent_end` events)
- Capabilities include `Programmatic`, `SessionBinding`, and `HeadlessPrompt`

This means for Pi, the orchestration works differently than Claude Code:
- Claude Code: stop hook blocks exit, injects context, Claude retries
- Pi: extension observes `agent_end`, calls `run:iterate` itself, injects next task as a message

Pi's loop-driver model is actually more reliable than Claude Code's stop-hook model because the extension controls the loop programmatically rather than relying on the LLM to call the right CLI commands.

## Process Definition: Concrete Example

From `library/tdd-quality-convergence.js` — the pattern for a real process:

```javascript
import { defineTask } from '@a5c-ai/babysitter-sdk';

// Task definitions use defineTask(id, buildFn)
// buildFn receives (args, taskCtx) and returns a TaskDef object
const myTask = defineTask('my-task-id', (args, taskCtx) => ({
  kind: 'agent',              // or 'shell', 'breakpoint', 'sleep'
  title: 'Human-readable name',
  agent: {
    name: 'agent-identifier',
    prompt: {
      role: 'description of expertise',
      task: 'what to accomplish',
      context: { /* structured metadata */ },
      instructions: ['step 1', 'step 2'],
      outputFormat: 'description of expected output'
    },
    outputSchema: { /* JSON Schema for structured output */ }
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/result.json`
  },
  labels: ['categorization', 'tags']
}));

// Process function orchestrates tasks
export async function process(inputs, ctx) {
  const result = await ctx.task(myTask, { feature: inputs.feature });
  
  await ctx.breakpoint({
    question: 'Human approval needed?',
    title: 'Gate Name'
  });
  
  // Parallel execution
  const [a, b] = await ctx.parallel.all([
    () => ctx.task(taskA, {}),
    () => ctx.task(taskB, {}),
  ]);
  
  // Convergence loop
  let quality = 0;
  while (quality < inputs.target) {
    const score = await ctx.task(scoreTask, {});
    quality = score.value;
  }
  
  return { success: true, quality };
}
```

## Example Build Process: What It Would Look Like

Each of the runbook's 12 steps becomes a `defineTask` with `kind: 'agent'`. The agent is instructed to run the Python script, interpret the output, and return structured results. The process function encodes the flow:

- Steps 1-4 are sequential (`await ctx.task(...)` one after another)
- Steps 5, 6, 7 run in parallel (`ctx.parallel.all([...])`) — optimize shard + build mappings + build metadata
- Steps 8-12 are sequential again (S3 sync must precede Iceberg build)
- Each major checkpoint has a `ctx.breakpoint()` mirroring the runbook's "what to check before proceeding"
- The new-shard decision (step 3) is a plain `if` in JavaScript — deterministic, not LLM-decided

The agent tasks are `kind: 'agent'` (not `kind: 'shell'`) because interpreting results requires judgment: "is zero new studies normal for a schema-fix release?" can't be answered by an exit code. The agent runs the script AND interprets the output, returning a structured verdict the process can gate on.

Shell tasks (`kind: 'shell'`) would be appropriate for deterministic steps like `git annex copy --to s3-export` where interpretation isn't needed — but even those benefit from agent tasks that can diagnose failures.

## Key Architectural Decisions for Pinard Integration

1. **Pi drives the loop, not the stop hook.** Babysitter's Pi adapter uses loop-driver mode — the extension controls iteration, not the LLM. This is more reliable for long-running data pipelines.

2. **Pi extension tracks effect state independently.** The extension records the current effectId when `run:iterate` dispatches a task, and can auto-post results if the LLM forgets `task:post`. This compensates for babysitter's prompt-only enforcement.

3. **Process definitions are project-specific.** `example-build.js` encodes Example's exact flow. Task definitions (`slurm.js`, `git-annex-sync.js`) are reusable across projects. No generic pipeline interpreter.

4. **NATS bridges the gap babysitter can't fill.** Babysitter is single-machine. Cross-environment coordination (HPC build complete -> trigger cloud release) flows through NATS, orchestrated by the conductor. Babysitter governs the worker; NATS governs the fleet.

5. **The conductor doesn't need babysitter.** The conductor's value is LLM judgment under uncertainty (error routing, environment selection, human escalation). Process-as-code doesn't fit that role. Babysitter is the inner loop; the conductor stays freeform.
