## ADDED Requirements

### Requirement: Provider-Neutral Pressoir Seam

All pressoir interactions (pull/merge requests, issues, comments, reviews, CI status,
labels, user resolution) SHALL flow through a single provider-neutral `Pressoir`
interface. No component — Go or extension — SHALL call `glab`, `gh`, or a
provider-specific HTTP API directly.

#### Scenario: A pressoir operation has exactly one implementation
- **GIVEN** any pressoir operation (e.g. open a change request, post a comment)
- **WHEN** it is invoked from the CLI, a watcher, or the extension
- **THEN** it is served by the `Pressoir` implementation for the resolved provider
- **AND** no call site branches on `glab`-vs-`gh` or embeds `api/v4`/GitHub REST paths

#### Scenario: Extension routes through aoc, not a git host CLI
- **WHEN** the conductor or worker extension needs a pressoir operation
- **THEN** it calls an `aoc pressoir …` subcommand
- **AND** the TypeScript code contains no `glab` or `gh` invocation

### Requirement: Neutral Domain Model

The pressoir model SHALL be provider-neutral: a change request SHALL be identified by a
`Number` (not `iid`), a repository by a `RepoRef{Host,Owner,Name}`, and CI state by a
neutral `CIStatus`. Provider-specific shapes (GitLab `iid`, subgroup paths,
`position[...]`, pipeline objects; GitHub `owner/repo`, check runs) SHALL be confined to
adapters.

#### Scenario: Callers use Number, not iid
- **WHEN** any caller references a change request or issue
- **THEN** it uses the neutral `Number`
- **AND** GitLab `iid` and GitHub `number` differences are handled inside the adapter

#### Scenario: CI gate is provider-neutral
- **WHEN** auto-merge evaluates whether a change request is mergeable
- **THEN** it reads a neutral `CIStatus` (success | failed | running | pending | none)
- **AND** the GitLab adapter derives it from pipeline status / `detailed_merge_status`
- **AND** the GitHub adapter derives it by aggregating check runs + required workflow runs

### Requirement: Config-Driven Provider Selection

The pressoir provider SHALL be selected by configuration, resolvable per vignoble and per
repository, defaulting to GitLab so existing deployments are unaffected.

#### Scenario: Existing GitLab deployment is unchanged
- **GIVEN** a vignoble with no `pressoir` field configured
- **WHEN** any pressoir operation runs
- **THEN** the GitLab provider is used with the existing host, group, and token
- **AND** behaviour is identical to before the seam existed

#### Scenario: A GitHub-backed repo uses the GitHub adapter
- **GIVEN** a repo configured with `pressoir: { provider: github }`, an org, host, and token env
- **WHEN** a worker opens a change request for that repo
- **THEN** the GitHub adapter opens a pull request via the GitHub API
- **AND** auto-merge gates on GitHub check runs

### Requirement: Capability Probing for Divergent Primitives

Where a planning or review primitive exists on one provider but not another (e.g.
GitLab group epics), the pressoir seam SHALL expose provider `Capabilities` so callers
adapt rather than fail.

#### Scenario: Epic promotion degrades gracefully on GitHub
- **GIVEN** a GitHub-backed vignoble (no native epics)
- **WHEN** the régisseur would promote work to an epic
- **THEN** `Capabilities()` reports epics unsupported
- **AND** the régisseur falls back to the configured alternative (milestone / Projects v2
  / task-list sub-issue) instead of erroring

### Requirement: Provider-Aware Agent Prompts

Agent prompts (worker, babysitter, conductor) SHALL NOT hard-code provider-specific
pressoir guidance. Instructions for opening change requests and referencing review
artifacts SHALL be templated from the resolved pressoir.

#### Scenario: Worker prompt matches the repo's pressoir provider
- **GIVEN** a worker spawned on a GitHub-backed repo
- **WHEN** its prompt describes how to open a change request
- **THEN** the guidance refers to pull requests and the GitHub flow
- **AND** contains no `glab api … merge_requests` instruction
