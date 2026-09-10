## Context

The conductor receives `issues_new` events via NATS but treats them as passive log entries (`deliverAs: "followUp"`). The event message is a one-liner that doesn't prompt investigation. The conductor needs to actively handle assigned issues by reading them, providing context, and asking the user whether to spawn a worker.

## Goals / Non-Goals

**Goals:**
- Conductor reacts to `issues_new` for assigned issues (no `auto-pinard` label) by presenting the issue and asking the user
- Use `deliverAs: "steer"` to wake the idle conductor when a new issue arrives
- Provide a rich message with issue details so the conductor can investigate

**Non-Goals:**
- Changing auto-spawn behavior for `auto-pinard` labeled issues (daemon handles those)
- Making the conductor spawn without user confirmation

## Decisions

### Decision 1: Use "steer" delivery for issues_new

**Choice:** Add `issues_new` to `DISPATCH_TYPES` so it wakes the conductor immediately.

**Rationale:** Assigned issues need prompt attention. "followUp" only delivers on the next user-initiated turn, which could be hours later.

### Decision 2: Rich message with investigation prompt

**Choice:** Format `issues_new` as a detailed message including issue title, description, URL, and labels, with an explicit instruction to investigate and ask the user.

**Rationale:** The conductor LLM needs enough context to read the issue, look at CI logs if applicable, and formulate a recommendation.

### Decision 3: Skip steer for auto-spawned issues

**Choice:** Only steer for issues where `auto_spawn` is false in the event data.

**Rationale:** Auto-spawned issues are already handled by the daemon. The conductor just needs the log entry.
