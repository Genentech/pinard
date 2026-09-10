## Purpose

This specification defines the conductor's event classification and routing. The conductor must receive events from all workers and parcelles but only invoke the LLM for events that require human attention or judgment. Informational events are silently dropped — enforcing "know but don't react" mechanically in extension code, not through prompting.

## Background

The conductor operates freeform: it's an interactive LLM session where the user talks to it directly. Events from workers are classified in the Pi extension's event handler (deterministic code) before deciding whether to deliver them to the LLM.

Previous problems solved:
1. **Interference** — the conductor reacted to process-governed worker events (investigating, sending messages to workers that don't need help)
2. **Context pollution** — informational events filled the context window with noise
3. **Unpredictable behavior** — you can't tell the LLM "know this but don't act" reliably

The solution: extension-level event classification. Deterministic code in `handleAgentEvent` classifies events and only delivers human-attention and judgment-needed events to the LLM. The conductor stays freeform for user interaction.

## Architecture

```
NATS events (all vignoble activity)
  │
  ▼
Extension event handler (deterministic classification)
  │
  ├─ Informational → silent (dashboard state only, LLM never sees it)
  ├─ Human-attention → delivered as steer + ACK-able
  ├─ Judgment-needed → delivered as steer (freeform workers only)
  └─ User interaction → pass through to freeform LLM
```

The conductor is a single freeform LLM session. Event classification happens in extension code — the LLM only receives events that need its attention. User interaction (typing in the conductor tmux) is unaffected by event handling.

## Event Routing Matrix

The daemon dispatches events to workers. The conductor observes events for human awareness. They never both act on the same event.

| Event | Process worker (conductor) | Freeform worker (conductor) | Who dispatches to worker? |
|---|---|---|---|
| pipeline_failed | Informational (process handles) | Informational (daemon handles) | Daemon → inbox |
| pipeline_cancelled | Informational (process breakpoint) | Informational (daemon handles) | Daemon → inbox |
| pipeline_passed | Human-attention (ACK) | Human-attention (ACK) | Nobody — notification only |
| review_comment | Informational (process handles) | Informational (daemon handles) | Daemon → inbox |
| mr_merged | Informational (process handles) | Informational | Daemon → inbox |
| mr_closed | Informational (process handles) | Informational | Daemon → inbox |
| auto_merged | Informational (process handles) | Informational | Daemon → inbox |
| needs_approval | Human-attention (ACK) | Human-attention (ACK) | Nobody — notification only |
| main_pipeline_passed | Human-attention (ACK) | Human-attention (ACK) | Nobody — notification only |
| main_pipeline_failed | Informational (process handles) | Informational (daemon handles) | Daemon → inbox |
| process_failed | Human-attention (ACK) | N/A | Nobody — needs human |
| breakpoint | Human-attention (ACK) | N/A | Nobody — needs human |
| circuit_breaker | Human-attention (ACK) | Human-attention (ACK) | Nobody — needs human |
| issues_new (non-auto) | N/A | Judgment-needed | Conductor decides |
| agent_idle | Informational | Informational | Nobody |
| session_ended | Informational | Informational | Nobody |

**Principle:** daemon dispatches, conductor observes. The conductor only wakes up for events that need human attention or judgment.

## Requirements

### Requirement: Event Classification

All events SHALL be classified into one of three categories by deterministic code (not LLM judgment):

| Category | LLM involved? | Examples |
|----------|---------------|----------|
| **Informational** | No — silent | process_task_started, process_task_completed, worker state changes, agent_idle, btw_reply |
| **Human-attention** | Yes — ACK-able alert | breakpoint, process_failed, circuit_breaker, needs_approval, pipeline_passed, main_pipeline_passed |
| **Judgment-needed** | Yes — LLM decides | pipeline_failed (freeform only), review_comment (freeform only), run_orphaned, issues_new (non-auto) |

#### Classification logic (implemented in extension)

```javascript
// Check KV for process field
const isProcessGoverned = !!kvAgentState.process;

// Human-attention: always surface
const HUMAN_ATTENTION_TYPES = new Set([
  "process_failed", "circuit_breaker",
  "needs_approval", "pipeline_passed", "main_pipeline_passed",
]);
if (type === "breakpoint" || type === "process_gate_pending") return "human_attention";
if (HUMAN_ATTENTION_TYPES.has(type)) return "human_attention";

// Process-governed workers: everything else is informational
if (isProcessGoverned) return "informational";

// Freeform workers: dispatch types need conductor judgment
  if (['pipeline_failed', 'review_comment'].includes(type)) {
    return 'judgment_needed';
  }

  return 'informational';
}
```

### Requirement: Informational Events — Dashboard Only

Informational events SHALL update an in-memory dashboard state without invoking the LLM. The dashboard is queryable on demand (via `/status`, `/parcelles`, or conductor tools).

```javascript
// No sendUserMessage, no LLM context consumed
function handleInformational(event) {
  dashboardState.update(event);
  // Optionally write to log file for audit
  appendToLog(event);
}
```

Dashboard state tracks per-parcelle:
- Active workers (session, process, current task)
- Recent completions (last N process results)
- Pending gates (breakpoints awaiting response)
- MR status summary (open, approved, merged)

### Requirement: Human-Attention Events — Formatted Alert

Human-attention events SHALL be surfaced to the user via the LLM, but the LLM's role is limited to **formatting the alert**, not deciding what to do.

```javascript
const alert = await ctx.task(formatAlert, {
  type: event.type,
  summary: event.data,
  parcelle: event.data.parcelle,
});
// Delivered as ACK-able event
```

The `formatAlert` agent task has a constrained prompt: "Format this event as a clear notification for the human. Do not investigate, do not take action, just present the information."

### Requirement: Judgment-Needed Events — LLM Decides

Judgment events invoke the LLM with full context to make a decision:

```javascript
const decision = await ctx.task(makeDecision, {
  type: event.type,
  context: getDashboardContext(event.data.parcelle),
  options: getValidActions(event.type),
});
// Execute the decision
await ctx.task(executeDecision, { decision });
```

Examples:
- `run_orphaned`: "This run was abandoned 2 hours ago. Workers available. Respawn?" → LLM decides based on parcelle priority
- Freeform worker `pipeline_failed`: "Worker has no process. Should I investigate or wait?" → LLM decides

### Requirement: User Interaction — Passthrough

When the human sends a message to the conductor, it bypasses the event loop entirely and goes to a freeform LLM turn. The LLM has access to the dashboard state as tools:

- `/status` — current parcelle/worker overview
- `/parcelles` — list active parcelles with progress
- `/events` — recent event history
- Gate responses — "approve breakpoint X", "reject breakpoint Y"

The freeform LLM can also trigger actions: spawn workers, kill workers, create parcelles, respond to gates.

### Requirement: Conductor Process Definition

```javascript
export async function process(inputs, ctx) {
  // Initialize dashboard
  const dashboard = { parcelles: {}, workers: {}, gates: [] };

  while (true) {
    const event = await ctx.task(waitForEvent, {
      events: ['*'], // all event types
    });

    const category = classifyEvent(event);

    switch (category) {
      case 'informational':
        updateDashboard(dashboard, event);
        break;

      case 'human_attention':
        await ctx.task(surfaceAlert, {
          event,
          dashboard: getDashboardSummary(dashboard, event.data?.parcelle),
        });
        break;

      case 'judgment_needed':
        const decision = await ctx.task(makeJudgmentCall, {
          event,
          dashboard: getDashboardSummary(dashboard, event.data?.parcelle),
          validActions: getActionsForEvent(event.type),
        });
        if (decision.action !== 'ignore') {
          await ctx.task(executeAction, { decision });
        }
        break;
    }
  }
}
```

### Requirement: Gate Handling

Breakpoints from process-governed workers SHALL be surfaced as pending gates in the dashboard and delivered as human-attention events:

```javascript
case 'breakpoint':
  dashboard.gates.push({
    parcelle: event.data.parcelle,
    runId: event.data.runId,
    effectId: event.data.effectId,
    question: event.data.question,
    receivedAt: new Date().toISOString(),
  });
  await ctx.task(surfaceAlert, { event, ... });
  break;
```

The human responds via the freeform channel: "approve gate for exo-cli-dev-42" → conductor resolves the gate via NATS publish to `…process.{name}.gate`.

### Requirement: Parcelle Context in Events

All events published by process-governed workers SHALL include `parcelle` and `runId` in the event payload. The conductor uses these to group events by workstream in the dashboard.

The worker extension SHALL add these fields to all published events:

```typescript
natsPublish(eventsSubject(type), {
  ...payload,
  _parcelle: BABYSITTER_PARCELLE || "",
  _runId: RUN_ID || "",
});
```

### Requirement: Coexistence with Freeform

The conductor process runs alongside freeform user interaction. This is possible because:

1. `waitForEvent` is a babysitter `event` effect — resolved by infrastructure, no LLM turn
2. User messages arrive via BTW/steer — independent of the event loop
3. The LLM is only invoked by `ctx.task()` for specific classified events
4. Between events, the conductor is idle — user can interact freely

The process never "blocks" user interaction. It's an event-driven loop that fires tasks only when events arrive.

## Dashboard State

```typescript
interface ConductorDashboard {
  parcelles: Record<string, {
    name: string;
    status: 'active' | 'archived';
    workers: Array<{
      session: string;
      runId: string;
      process: string;
      currentTask: string;
      state: 'running' | 'waiting' | 'completed' | 'failed';
    }>;
    recentCompletions: Array<{ runId: string; result: any; at: string }>;
    openMRs: Array<{ iid: number; project: string; status: string }>;
  }>;
  pendingGates: Array<{
    parcelle: string;
    runId: string;
    effectId: string;
    question: string;
    receivedAt: string;
  }>;
  recentEvents: Array<{ type: string; parcelle: string; at: string; summary: string }>;
}
```

## Migration from Current Conductor

The current conductor:
- Receives all events via NATS consumer
- Delivers every event to the LLM via `sendUserMessage`
- The LLM decides what to do (investigate, send_message to worker, ignore)

The new conductor:
- Still receives all events via NATS consumer
- Classifies them deterministically (no LLM)
- Only invokes LLM for human-attention and judgment events
- Maintains dashboard state for on-demand queries
- User interaction is separate from event handling

### Migration steps

1. Replace the NATS event handler in `pi-extension/pinard/index.ts` with the babysitter event loop
2. Move `handleAgentEvent` logic into the classification function
3. Dashboard state replaces the current `agentEvents` array
4. Existing conductor tools (`/status`, `list_workers`) read from dashboard state
5. ACK-able events become gates or human-attention alerts in the process

## Open Questions

1. **Process lifecycle** — The conductor process never completes (infinite event loop). Should it use a max-iterations guard, or does babysitter support infinite processes? Need to verify with babysitter SDK.

2. **User message routing** — How does a user message (typed in the conductor tmux) reach the freeform LLM when the babysitter process is active? Need to understand how Pi routes user input vs process tasks.

3. **Dashboard persistence** — Should dashboard state survive conductor restarts? If it's in-memory, it's lost on restart. Could use a file or KV bucket.

4. **Multiple vignobles** — The conductor handles events from one vignoble. If events from multiple vignobles need unified handling, the dashboard needs cross-vignoble awareness.

5. **Event backlog on startup** — When the conductor starts, there may be pending events from while it was down (JetStream durable consumer). The process should handle a burst of events without invoking the LLM for each one (batch informational, deduplicate alerts).
