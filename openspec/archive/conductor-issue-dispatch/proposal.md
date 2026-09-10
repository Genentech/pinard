## Why

When an issue is assigned to pinard (without `auto-pinard` label), the daemon publishes an `issues_new` event but nobody acts on it. The conductor receives the event for its dashboard log but does nothing — the user has to manually notice and decide to spawn. This defeats the purpose of assigning issues to pinard: the expectation is that pinard investigates and asks the user whether to proceed.

## What Changes

- The conductor reacts to `issues_new` events by reading the issue, investigating the context (CI logs, related code), and presenting a summary to the user with a proposal to spawn a worker
- The conductor asks for user confirmation before spawning — this is not auto-spawn, it's assisted dispatch
- If the issue has `auto-pinard` label, the daemon handles it directly (unchanged)
- If the issue is assigned to pinard without the label, the conductor handles the interactive dispatch

## Capabilities

### New Capabilities

- `conductor-issue-dispatch`: Conductor reacts to `issues_new` events — reads the issue, investigates context, presents summary, asks user for spawn confirmation

### Modified Capabilities

- `conductor`: Conductor gains active event handling for `issues_new` — injects issue content into the conversation as an actionable prompt instead of passively logging it

## Impact

- **Modified extension**: `pi-extension/pinard/index.ts` — `handleAgentEvent` for `issues_new` type injects a user-facing prompt
- **No daemon changes**: daemon continues to publish events and auto-spawn for `auto-pinard` labeled issues
- **UX change**: conductor will interrupt idle state to present new issues to the user
