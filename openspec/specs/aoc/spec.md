## Purpose

The `aoc` CLI binary: commands for spawning agents, watching and tracking merge requests, and sending notifications over NATS.
## Requirements
### Requirement: Agent Spawning (spawn)

The spawn command SHALL launch a background Claude agent in a tmux session with its own git worktree.

#### Scenario: Basic spawn
- **WHEN** `aoc spawn --project exo-cli --prompt "fix the bug"`
- **THEN** creates a git worktree in the project's repo directory
- **AND** ensures the vignoble tmux server (socket) exists (creates if needed)
- **AND** creates a tab named `{session-name}` in the vignoble workspace
- **AND** sends the worker command (`pinard --worker ...`) to the tab's pane via `pane.send_text`
- **AND** publishes worker state to KV `pinard-agents.{session-name}`

#### Scenario: Spawn with issue
- **WHEN** `--issue 42` is provided
- **THEN** fetches issue #42 details from GitLab
- **AND** includes the issue description in the agent's prompt

#### Scenario: Spawn with session name
- **WHEN** `--session-name custom-name` is provided
- **THEN** uses that name for the tmux session instead of auto-generated

#### Scenario: Spawn with target branch
- **WHEN** `--target-branch develop` is provided
- **THEN** the worktree is created from `develop` instead of the default branch

#### Scenario: Worktree creation
- **WHEN** spawning an agent
- **THEN** creates branch `agent/{session-name}` from the target branch
- **AND** worktree is at `{project-path}/.worktrees/{session-name}`

#### Scenario: tmux not available
- **WHEN** the tmux server is not available
- **THEN** the spawn command fails with error: "tmux is not available"

#### Scenario: Spawn output
- **WHEN** spawn succeeds
- **THEN** logs: "Spawned: {name} on {project} in {dir}"
- **AND** prints attach instructions: "View in tmux (socket pinard-{vignoble})"

---

### Requirement: MR Watcher (watch-mrs)

The watch-mrs command SHALL poll tracked MRs for state changes, new comments, and pipeline results.

#### Scenario: MR merged
- **WHEN** a tracked MR's state changes to `merged`
- **THEN** publishes `pinard.{vignoble}.agents.{session}.events.mr_merged` with `{mr: N, project: "..."}`
- **AND** closes the tmux session for that worker
- **AND** removes the tracking entry from KV and state file

#### Scenario: MR closed (not merged)
- **WHEN** a tracked MR's state changes to `closed`
- **THEN** publishes `pinard.{vignoble}.agents.{session}.events.mr_closed`
- **AND** closes the tmux session and removes tracking entry

#### Scenario: Circuit breaker
- **WHEN** `pipeline_fail_count` reaches 5
- **THEN** publishes `pinard.{vignoble}.agents.{session}.events.circuit_breaker`
- **AND** closes the tmux session
- **AND** removes tracking entry

#### Scenario: Dead session cleanup
- **WHEN** the tracked session's KV entry shows `state: "stopped"` or the tmux session doesn't exist
- **THEN** removes the tracking entry

---

### Requirement: MR Tracking (track-mr)

The track-mr command SHALL register a merge request for monitoring by the MR watcher.

#### Scenario: Auto-detect CWD
- **WHEN** `aoc track-mr` is called from a worker session without explicit `--project`
- **THEN** queries the tmux pane's current working directory via the tmux API
- **AND** uses the CWD to resolve the git repo and project

---

### Requirement: Notifications (notify)

The notify command SHALL send notifications to the user via NATS.

#### Scenario: Notify
- **WHEN** `aoc notify --message "task complete"`
- **THEN** publishes to `pinard.{vignoble}.notifications` NATS subject
- **AND** appends to `.state/notifications.log`

### Requirement: Parcelle-Leading Worker Session Naming

Auto-generated worker session names SHALL lead with the parcelle id so that a raw session listing (e.g. `tmux ls` or the prefix+`f` session picker) is self-describing and filterable by parcelle.

#### Scenario: Auto-generated name format
- **WHEN** `aoc spawn` generates a worker session name (no explicit `--session-name`)
- **THEN** the name SHALL lead with the parcelle id, followed by project and a disambiguator, e.g. `<parcelle>--<project>-<id><rand>`
- **AND** `<id>` SHALL be the issue IID when spawning for an issue, otherwise a time-based token
- **AND** the name SHALL retain a collision-avoidance suffix
- **AND** the name SHALL NOT include the redundant vignoble token (the tmux socket `pinard-<vignoble>` already scopes it)

#### Scenario: Filter workers by parcelle
- **WHEN** an operator lists worker sessions and filters by a parcelle prefix
- **THEN** the sessions belonging to that parcelle SHALL be selectable by their leading token

#### Scenario: Explicit session name is respected
- **WHEN** `--session-name` is provided
- **THEN** that name SHALL be used verbatim (subject to name-safety sanitization)

### Requirement: tmux-Safe Name Sanitization

Any parcelle id or session name used as a tmux target SHALL be sanitized so it contains no tmux target separators.

#### Scenario: Forbidden characters are sanitized
- **WHEN** a parcelle id or session name would contain `.` or `:` (tmux target separators)
- **THEN** those characters SHALL be replaced/removed before the name is used with tmux
- **AND** the sanitized name SHALL remain stable (deterministic) for the same input

### Requirement: Parcelle-Scoped Session File Resolution

`aoc spawn` SHALL determine parcelle from `--parcelle` (defaulting to the project name), record it in KV, and resolve parcelle-scoped paths under `parcelles/<parcelle>/`.

#### Scenario: Parcelle defaulting and KV
- **WHEN** `aoc spawn` runs with or without `--parcelle`
- **THEN** the effective parcelle SHALL be the provided value, or the project name when omitted
- **AND** the effective parcelle SHALL be written to KV `pinard-agents.{session-name}.parcelle`

#### Scenario: Run directory under parcelle
- **WHEN** a run directory is created for a spawn
- **THEN** it SHALL be located under `parcelles/<parcelle>/runs/<runID>/`

### Requirement: Credential bootstrap (uncork)
`aoc` SHALL provide an `uncork` subcommand that materializes a credential/config
bundle for a sandboxed worker. It SHALL accept the bundle from a URL
(`--url`, default `$PINARD_UNCORK_URL`) or stdin, expect a JSON manifest of the
form `{ "files": [ { "path", "mode"?, "content", "encoding"? } ] }`, and write
each file under `$HOME` (paths relative to `$HOME`) with the declared mode
(default `0600`), creating parent directories.

#### Scenario: Materialize a bundle from a URL
- **WHEN** `aoc uncork --url <endpoint>` is run and the endpoint returns a valid
  manifest
- **THEN** each listed file is written under `$HOME` with its mode
- **AND** the command exits zero

#### Scenario: Fail fast on a bad bundle
- **WHEN** the endpoint returns a non-2xx status or malformed JSON
- **THEN** `aoc uncork` prints an error and exits non-zero without writing partial
  secrets that would be used as complete

### Requirement: Proxy provider seeding (ensure-proxy-provider)
`aoc ensure-proxy-provider` SHALL make pi aware of the LLM proxy provider in a
`--containall` sandbox whose `~/.pi` starts empty. It SHALL build the provider
registry (`~/.pi/agent/models.json`) from image-baked non-secret defaults (base
URL, headers, model ids) and mint the provider credential (`~/.pi/agent/auth.json`)
from `PINARD_POUR_URL`. When `~/.claude/settings.json` is present, it MAY continue
to source the provider from that file (backward compatible). It SHALL remain
idempotent: if `models.json` already defines a proxy provider, it is left as-is.

#### Scenario: Seed from baked defaults + pour URL
- **WHEN** `aoc ensure-proxy-provider` runs with `PINARD_POUR_URL` set and no
  `~/.claude/settings.json`
- **THEN** `~/.pi/agent/models.json` is written from baked defaults
- **AND** `~/.pi/agent/auth.json` contains a token minted from `PINARD_POUR_URL`

#### Scenario: Backward-compatible settings.json path
- **WHEN** `~/.claude/settings.json` is present with an `apiKeyHelper`
- **THEN** `ensure-proxy-provider` still derives the provider from it

#### Scenario: Idempotent when already configured
- **WHEN** `~/.pi/agent/models.json` already defines a proxy provider
- **THEN** `ensure-proxy-provider` makes no changes

