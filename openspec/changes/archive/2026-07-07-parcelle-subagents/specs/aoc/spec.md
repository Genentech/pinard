## ADDED Requirements

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
