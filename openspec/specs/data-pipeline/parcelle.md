## Purpose

This specification defines the **parcelle** — a named, persistent grouping of related work within a vignoble. Parcelles provide the missing layer between "vignoble" (everything) and "individual worker" (one issue), enabling coherent workstreams that span multiple issues, workers, and sessions.

## Background

Today, pinard workers are isolated: each spawns for one issue, works in one worktree, and dies after merge. There's no persistent concept of "the semantic search effort" or "the babysitter integration" — just individual issues. The conductor has no memory of what workstream a conversation belongs to, and workers don't inherit broader context from related work.

Parcelles solve this by giving workstreams identity, persistence, and scope.

## Concept

A **parcelle** (French: a plot within a vineyard) is a named area of focused work within a vignoble.

- Created by the conductor conversationally ("let's work on semantic search")
- Persists across sessions, workers, and days
- Groups related issues, runs, and context
- Multiple parcelles are active simultaneously
- Babysitter run journals are scoped to their parcelle

## Requirements

### Requirement: Parcelle Lifecycle

The conductor SHALL manage parcelle creation, selection, and archival.

#### Scenario: Conductor creates a parcelle
- **WHEN** the user starts a new topic ("let's work on babysitter integration")
- **THEN** the conductor SHALL create a new parcelle or ask if it relates to an existing one
- **AND** the parcelle is stored in `vignoble/parcelles/<name>/parcelle.yaml`
- **AND** subsequent spawned workers inherit the active parcelle

#### Scenario: Conductor selects an existing parcelle
- **WHEN** the user references existing work ("back to semantic search")
- **THEN** the conductor SHALL activate the matching parcelle
- **AND** gain context from the parcelle's history (runs, issues, decisions)

#### Scenario: Parcelle archival
- **WHEN** all issues in a parcelle are closed and all runs completed
- **THEN** the conductor MAY suggest archiving the parcelle
- **AND** archived parcelles remain on disk but are not listed as active

### Requirement: Parcelle Storage

Parcelles SHALL be stored in the vignoble with this layout:

```
vignoble-exohub/parcelles/
  babysitter/
    parcelle.yaml            ← metadata: description, created, status, issues
    runs/
      pinard-dev-123/        ← babysitter run journal for issue #123
        run.json
        journal/
        tasks/
        state/
      pinard-dev-125/        ← another run in the same parcelle
        ...
  semantic-search/
    parcelle.yaml
    runs/
      exo-cli-dev-42/
      exo-cli-dev-45/
```

This replaces the per-worktree `.a5c/runs/` directory. Runs are durable, survive worker crashes and worktree cleanup.

#### `parcelle.yaml`

```yaml
name: babysitter
description: Integrate babysitter as worker-level process orchestrator
status: active          # active | archived
created: 2026-06-02
issues:
  - project: pinard
    iid: 123
    title: "Phase 1: transparent integration"
  - project: pinard
    iid: 125
    title: "Phase 2: process loading"
```

### Requirement: Run ID Convention

Run IDs SHALL be deterministic and human-readable, derived from the spawn context:

| Spawn trigger | Run ID format | Example |
|---|---|---|
| Issue-driven | `<project>-<process>-<issueIID>` | `exo-cli-dev-42` |
| Scheduled (daily) | `<project>-<process>-<schedule>-<YYYYMMDD>` | `example-example-build-daily-20260602` |
| Scheduled (weekly) | `<project>-<process>-<schedule>-<YYYY>W<WW>` | `example-example-build-weekly-2026W23` |
| Manual (no issue) | `<project>-<process>-<sessionName>` | `exo-cli-dev-exohub-exo-cli-4217` |

Run IDs are passed to babysitter via `--run-id`. If a run with that ID already exists in the parcelle, the extension resumes it. If not, it creates it.

### Requirement: Run Recovery via Journal Scanning

The daemon SHALL detect orphaned incomplete runs by scanning parcelle journals, not by relying on KV state.

**Rationale:** KV state is ephemeral — it's deleted on graceful exit (`session_end`), which means a paused worker (intentional stop) and a crashed worker (unintentional death) both leave the run unfinished. The journal is the durable truth: if `RUN_COMPLETED` is absent and no active worker owns the run, it's an orphan.

#### Requirement: Orphan detection

The daemon SHALL periodically scan parcelle run directories:

```
for each vignoble/parcelles/*/runs/*/:
  1. Does the journal contain RUN_COMPLETED or RUN_FAILED? → skip (finished)
  2. Is there an active worker in KV with this runId? → skip (in progress)
  3. Otherwise → orphaned run, candidate for recovery
```

An active worker is one whose KV entry has `runId` matching this run and whose tmux session is alive. This handles:

| Situation | KV state | Journal state | Daemon action |
|---|---|---|---|
| Worker running normally | present, `runId` set | incomplete | Skip (owned) |
| Worker crashed | present but tmux dead | incomplete | Orphan → recover |
| Worker gracefully paused | deleted (`session_end` fired) | incomplete | Orphan → recover |
| Worker finished and exited | deleted | `RUN_COMPLETED` | Skip (done) |
| Worker failed | deleted or present | `RUN_FAILED` | Skip (failed, surface to conductor) |

#### Scenario: Worker crash recovery
- **GIVEN** a worker was running process `dev` for issue #42 in parcelle `semantic-search`
- **AND** the run journal is at `vignoble/parcelles/semantic-search/runs/exo-cli-dev-42/`
- **WHEN** the worker crashes (tmux dies, OOM, timeout)
- **THEN** the daemon's next scan finds the run: no `RUN_COMPLETED`, no active worker with `runId: exo-cli-dev-42`
- **AND** the daemon SHALL respawn: `aoc spawn --project exo-cli --process dev --issue 42 --parcelle semantic-search`
- **AND** the babysitter extension SHALL find the existing run at the deterministic path
- **AND** `run:iterate` replays the journal (completed tasks fast-forward)
- **AND** execution resumes from the first unresolved effect

#### Scenario: Intentional pause and later resume
- **GIVEN** a worker is paused (user exits the session, or conductor kills it to free resources)
- **AND** `session_end` fires, KV entry is deleted
- **WHEN** the daemon's next scan finds the orphaned run
- **THEN** the daemon SHALL NOT auto-respawn immediately
- **AND** instead SHALL publish a `run_orphaned` event to NATS: `pinard.{vignoble}parcelles.{parcelle}.events.run_orphaned`
- **AND** the conductor SHALL decide: respawn now, wait, or mark as intentionally paused

#### Scenario: No recovery needed for completed runs
- **GIVEN** a run with `RUN_COMPLETED` in the journal
- **WHEN** the daemon scans it
- **THEN** it SHALL skip the run (no recovery needed)

#### Requirement: Conductor-gated recovery

The daemon SHALL NOT auto-respawn orphaned runs by default. Instead, it SHALL publish orphan events and let the conductor (or a human) decide.

**Rationale:** Not all orphans should be respawned. A paused data pipeline might be intentionally waiting for another pipeline. A crashed dev worker on a minor issue might not be worth respawning. The conductor has the context to decide.

The daemon MAY auto-respawn if the parcelle or process definition is configured with `auto_recover: true`:

```yaml
# parcelle.yaml
name: example-build
auto_recover: true    # daemon auto-respawns orphaned runs in this parcelle
```

### Requirement: Default Parcelle

Every vigne SHALL have an implicit default parcelle named after the project. One-off work lands here without requiring `--parcelle`.

```bash
# One-off: no --parcelle, no --issue → uses default parcelle "exo-cli"
aoc spawn --project exo-cli --process dev --prompt "fix typo in README"

# Intentional workstream: explicit --parcelle
aoc spawn --project exo-cli --process dev --issue 42 --parcelle semantic-search
```

Default parcelle characteristics:
- Name matches the project name (e.g. `exo-cli`)
- Created automatically on first use (no `parcelle.yaml` needed)
- Run IDs fall back to session name (not deterministic, not resumable)
- Daemon does NOT auto-recover orphaned runs in default parcelles
- Serves as a bin for one-offs — no ceremony, no long-lived expectations

```
vignoble-exohub/parcelles/
  exo-cli/                    ← default parcelle (one-offs for exo-cli)
    runs/
      exo-cli-dev-exohub-exo-cli-4217/   ← one-off, session-named
      exo-cli-dev-exohub-exo-cli-5301/   ← another one-off
  semantic-search/            ← named parcelle (intentional workstream)
    runs/
      exo-cli-dev-42/         ← issue-driven, deterministic, resumable
      exo-cli-dev-45/
```

### Requirement: Worker Spawn with Parcelle

The `aoc spawn` command SHALL accept `--parcelle` and `--issue` flags:

```bash
aoc spawn --project exo-cli --process dev --issue 42 --parcelle semantic-search
```

- `--parcelle` selects the parcelle (created if not exists). Defaults to the project name.
- `--issue` identifies the GitLab issue driving this work
- Together they determine the `--runs-dir` and `--run-id` for babysitter

When the conductor spawns a worker, it passes the active parcelle automatically.

### Requirement: Daemon Auto-Spawn with Parcelle

When the issue watcher triggers `autoSpawnForIssue`, the daemon SHALL determine the parcelle from the issue's labels or context:

- If the issue has a label matching an active parcelle name → use that parcelle
- If not → use the default parcelle (project name)

### Requirement: Conductor Context from Parcelle

When a parcelle is active, the conductor SHALL have access to:

| Context | Source |
|---|---|
| Issues in this workstream | `parcelle.yaml` issues list |
| Run history and outcomes | `runs/*/state/output.json` |
| Which tasks succeeded/failed | `runs/*/journal/` |
| Current in-progress work | Active workers with this parcelle in KV state |

This enables the conductor to answer: "what's the status of semantic search?" by reading the parcelle's runs rather than querying each worker individually.

### Requirement: NATS Subjects with Parcelle

Process-scoped NATS subjects MAY include the parcelle for additional filtering:

```
pinard.{vignoble}.agents.{session}.process.{processName}.inbox
```

The parcelle is NOT in the NATS subject (it would make subjects too long and subjects don't need it — routing is per-session). Instead, parcelle is metadata in the KV state and event payloads.

### Requirement: Parcelle in KV State

The worker's KV state SHALL include the parcelle name:

```json
{
  "project": "exo-cli",
  "process": "dev",
  "parcelle": "semantic-search",
  "state": "running",
  "tempo": "active",
  "runId": "exo-cli-dev-42"
}
```

## Migration

### From `.a5c/` in worktree to parcelle in vignoble

The babysitter extension's `--runs-dir` changes from:
```
<worktree>/.a5c/runs
```
to:
```
<vignoble>/parcelles/<parcelle>/runs
```

The `BABYSITTER_RUNS_DIR` env var is set by `bin/pinard` based on `--parcelle`:
```bash
export BABYSITTER_RUNS_DIR="$VIGNOBLE/parcelles/$WORKER_PARCELLE/runs"
```

## Open Questions

1. **Parcelle discovery for auto-pinard issues** — How does the daemon map an issue to a parcelle? Label matching is simple but requires users to label issues. Alternative: conductor assigns parcelle when it first sees the issue event.

2. **Cross-project parcelles** — A parcelle like "babysitter" spans pinard + example-workers. Should runs from both projects live in the same parcelle? If so, the run ID needs the project prefix (already proposed).

3. **Parcelle memory integration** — Should the parcelle have its own engram/memory scope? "What we decided about the event effect" is parcelle-level context, not vignoble-level.

4. **Conductor UI** — How does the conductor present parcelles? A dashboard command (`/parcelles`)? Auto-suggest on conversation start? Status command shows per-parcelle progress?
