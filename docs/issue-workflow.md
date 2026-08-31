# Issue Workflow

How GitLab issues reach pinard agents.

## Detection: Assignee-based

The issue watcher (`aoc watch-issues`) scans all vignes for open issues **assigned to the pinard GitLab user** (`PINARD_GITLAB_USER` from credentials.yaml).

Issues are NOT detected by label — the only trigger is the assignee.

## Two modes: manual and auto-spawn

### Manual (default)

1. Create a GitLab issue in any watched project
2. Assign it to the pinard user (e.g. `your-bot-user`)
3. `aoc watch-issues` detects it → publishes `issues_new` event to NATS
4. Conductor receives the event and **asks the human**: "New issue #X — want me to spawn an agent?"
5. Human confirms → conductor spawns

### Auto-spawn (with `auto-pinard` label)

1. Create a GitLab issue in any watched project
2. Assign it to the pinard user **AND** add the label `auto-pinard`
3. `aoc watch-issues` detects it → publishes `issues_new` with `auto_spawn: true`
4. Conductor **automatically spawns** an agent (code-level, no LLM judgment)
5. Conductor LLM receives an informational message: "[auto-spawn] Issue #X — agent spawned"

The conductor LLM does NOT need to decide — the spawn happens before the message reaches it.

## What the agent receives

The spawned agent's prompt includes:

- Issue title, description, and URL
- Instruction to investigate and fix
- `glab api` command to mark the issue as `in-progress` when work starts

## Issue lifecycle

| Stage | What happens |
|-------|-------------|
| **Created** | Human creates issue, assigns to pinard |
| **Detected** | `aoc watch-issues` picks it up, publishes NATS event |
| **Spawned** | Conductor spawns agent (manual or auto) |
| **In progress** | Agent marks issue as `in-progress` via GitLab API |
| **Completed** | Agent opens MR, notifies conductor via `aoc notify` |
| **Closed** | Issue watcher detects closure, updates state |

## Configuration

### credentials.yaml

```yaml
gitlab:
  user: your-bot-user    # GitLab username for the pinard service account
```

The watcher uses this username in the API: `?assignee_username=your-bot-user&state=opened`.

### GitLab setup

1. The pinard user must be a **Developer** (or higher) on the project
2. No special label configuration needed for manual mode
3. For auto-spawn: create the label `auto-pinard` in the project (any color)

### Vignoble

No per-project config needed. Any project listed in `vignes.yaml` with a `repo:` field is scanned.
