---
title: Authoring Processes
weight: 20
group: Building on Pinard
---

A **babysitter process** is the definition of a
[semi-deterministic loop](/docs/semi-deterministic-loop/). Writing one is how you
teach Pinard to do something new — it is the point at which Pinard becomes *an
engine you build on* rather than a fixed tool.

## Where a process lives

A process is a JavaScript module, versioned **with the code it operates on**:

```
<repo>/pinard/<process>/process.js
```

At launch the worker resolves the process file (exposed to the loop as
`BABYSITTER_PROCESS_PATH`); set `PINARD_PROCESS_FILE` to point at another file when
iterating locally. Spawn a worker on a process with `--process <name>`.

## Anatomy

A process has two parts: **task definitions** and the **process function** that
sequences them.

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/authoring-process-anatomy.jpg" alt="An open sketched process ledger with bounded task cards and structured output on the left, deterministic branching and a breakpoint gate on the right, and a resumable journal ribbon below.">
    <span class="doc-figure-label charcoal" style="--x: 27%; --y: 5%;">Task definitions</span>
    <span class="doc-figure-label mustard" style="--x: 72%; --y: 5%;">Deterministic process function</span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 17%; --y: 69%;">Typed task output</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 81%; --y: 22%;">Branch on typed result</span>
    <span class="doc-figure-label terracotta" style="--x: 90%; --y: 57%;">Breakpoint gate</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 86%; --y: 72%;">Terminal result</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 50%; --y: 96%;">Journaled checkpoints · resume at the next incomplete task</span>
  </div>
  <figcaption><strong>Judgment is bounded; control remains code.</strong> Tasks may use an LLM internally, but their output contract, ordering, branches, gates, and terminal result stay explicit.</figcaption>
  <ul class="doc-figure-legend" aria-label="Process anatomy">
    <li><span class="doc-figure-key terracotta">T</span><span><strong>Task</strong> — one bounded unit of work with typed inputs and outputs.</span></li>
    <li><span class="doc-figure-key">JS</span><span><strong>Control flow</strong> — plain JavaScript determines sequence and branches.</span></li>
    <li><span class="doc-figure-key charcoal">G</span><span><strong>Breakpoint</strong> — pauses before a risky transition and records the verdict.</span></li>
    <li><span class="doc-figure-key">J</span><span><strong>Journal</strong> — completed tasks are replayed, not repeated, after resume.</span></li>
  </ul>
</figure>

### Tasks — the steps

Define each step with `defineTask`. An **agent task** (`kind: 'agent'`) is a bounded
LLM turn with a typed output contract:

```js
import { defineTask } from '@a5c-ai/babysitter-sdk';

const fetchIssue = defineTask('fetch-issue', (args) => ({
  kind: 'agent',
  title: 'Fetch issue content',
  agent: {
    name: 'issue-fetcher',
    prompt: {
      role: 'A developer fetching issue details',
      task: `Use read_issue to fetch issue #${args.issueId} on "${args.project}".`,
    },
    // The model MUST return this shape — the loop can rely on it downstream.
    outputSchema: {
      type: 'object',
      properties: {
        title: { type: 'string' },
        description: { type: 'string' },
      },
      required: ['title', 'description'],
    },
  },
}));
```

The `outputSchema` is what keeps a non-deterministic step usable: the model may
*write* freely, but it must *return* a predictable structure the rest of the loop
consumes.

### The process function — the control flow

The exported `process(inputs, ctx)` function is **deterministic code**. It decides
the order, branches, and stopping condition; it only reaches into the model by
running a task:

```js
export async function process(inputs = {}, ctx) {
  const { project, parcelle, runId } = inputs;

  // Run an agent step and get its typed result.
  const issue = await ctx.task(fetchIssue, { issueId: inputs.issueId, project });

  // Deterministic branching on the model's structured output.
  const plan = await ctx.task(analyzeWork, { issue, project });

  await ctx.task(implement, { plan: plan.plan, affectedFiles: plan.affectedFiles });

  let tests = await ctx.task(runTests, {});
  if (!tests.passed) {
    // A gate: pause for a verdict (human or conductor) before continuing.
    await ctx.breakpoint({
      question: `Tests still failing after a fix attempt — retry, skip, or abort?`,
    });
  }

  const mr = await ctx.task(openMR, { project, runId });
  return { mrIid: mr.mrIid, status: 'opened' };
}
```

The primitives you compose:

| Primitive | Purpose |
|-----------|---------|
| `ctx.task(def, args)` | Run a step (agent turn or deterministic task); returns its typed result. Journaled. |
| `ctx.breakpoint({ question })` | A **gate** — pause the loop for a verdict/decision. The worker asks the operator (interactively) and records their decision; set `PINARD_BREAKPOINT_AUTO_APPROVE=1` to auto-approve gates for fully unattended runs. Journaled. |
| plain JS (`if`, `for`, `try`) | Deterministic control flow — the "semi-deterministic" half. |
| `return` | Terminal condition — the loop's honest outcome. |

## Journaling & resume — for free

Every `ctx.task` and `ctx.breakpoint` is recorded in the run journal under
`<runs-dir>/<run-id>/`. You do **not** write resume logic: because the run id is
stable, re-invoking the process replays the journal, skips completed steps, and
continues from the next one. Design steps to be **idempotent** where a re-run could
repeat side effects (e.g. check "does the MR already exist?" before opening one).

## Design guidelines

- **Push judgment into small steps.** Each agent task should do one bounded thing
  with a clear output contract — not "here's everything, go."
- **Keep control flow in code.** Branch on structured outputs, not on re-reading the
  model's prose.
- **Gate the risky transitions.** Use `ctx.breakpoint` where a wrong step is
  expensive or irreversible.
- **Make steps idempotent.** Assume any step may run twice after a resume.
- **Emit knowledge.** Save decisions and discoveries to [memory](/docs/memory/) so
  the next run of this process starts smarter.

## Reference process

The built-in [SWE process](/docs/swe-process/) (`processes/swe.js`) is a complete,
production example: fetch → analyze → implement → test → open MR → tend pipelines.
Read it alongside this page as a template.
