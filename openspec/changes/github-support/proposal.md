## Why

**Pinard is welded to GitLab.** Every pressoir interaction — opening merge requests,
polling issues, posting review comments, gating auto-merge on pipeline status,
promoting work to epics — assumes GitLab's API, object model, and CLI (`glab`). There
is no provider seam: git host access happens through **two parallel, GitLab-only paths**
that do not share a model:

- `internal/gitlab/client.go` — raw HTTP to `https://<host>/api/v4/<path>` with a
  `PRIVATE-TOKEN` header and GitLab-shaped JSON models (`internal/gitlab/types.go`).
  A *partial* abstraction used by the Go watchers and several `cmd/aoc` commands.
- **Direct `glab` shell-outs** that bypass that client entirely — in Go
  (`cmd/aoc/cmd_spawn.go`, `internal/watcher/issues.go`, `internal/watcher/mrs.go`)
  **and** in the extension (`pi-extension/pinard/index.ts`, `pi-extension/shared/tools.ts`).

The result is GitLab semantics leaking across ~a dozen call sites and into the agent
**prompt corpus** (worker/babysitter prompts literally instruct `glab api … merge_requests`).
Onboarding a team whose code lives on **GitHub** is currently impossible without a fork.

This change introduces a **pressoir-provider abstraction** so Pinard can drive **GitHub in
addition to GitLab**, selected per vignoble/repo by configuration — without duplicating
`glab`-vs-`gh` branching at every call site.

## What Changes

### A single pressoir seam (`Pressoir` interface)

Introduce a provider-neutral `Pressoir` interface with a **neutral domain model** (a
`PullRequest` with `Number` — not `iid`; `Issue`, `Comment`, `Review`, `CIStatus`,
`RepoRef`). Every pressoir call site — Go **and** the extension — routes through it. The
extension stops shelling out to `glab` directly and instead calls a thin
`aoc pressoir …` subcommand surface, so there is exactly **one** implementation of each
operation.

Ship the seam first with **only the existing GitLab behaviour behind it** (a pure
refactor, no behaviour change), then add the GitHub adapter. Provider selection is
config-driven (`pressoir: { provider: gitlab | github }`, host, org/group, token env)
resolvable per vignoble and per repo.

### Map the divergent semantics

| concept | GitLab (today) | GitHub (new adapter) |
|---|---|---|
| change unit | Merge Request, per-project `iid` | Pull Request, repo `number` (PRs are issues) |
| project ref | `group%2Fsub%2Fproject` (subgroups) | `owner/repo` (no subgroups) |
| comments | notes / resolvable discussions | issue comments / review threads |
| inline review | `position[new_path,new_line]` | `path` + `line` + `commit_id` (reviews API) |
| approvals | MR approvals (`approve`, `approved_by`) | PR reviews (`APPROVE` event) |
| CI gate | pipeline status + `detailed_merge_status` + `merge_when_pipeline_succeeds` | check/workflow runs + `auto_merge` |
| planning | group **epics** | parent issue + **native sub-issues** (task-list fallback) |
| auth | PAT + `PRIVATE-TOKEN` | **fine-grained PAT** + `Authorization: Bearer` (GitHub App deferred) |
| change events | REST polling (~1/min) | REST/GraphQL polling, **rate-limit-aware** (ETag + GraphQL batching; webhooks not viable) |

### Provider-aware prompts

The embedded GitLab-specific guidance in worker/babysitter/conductor prompts (how to
open MRs via `glab api …`, "MR" terminology, the "glab API host ≠ ssh push remote"
nuance) becomes **provider-aware templating** driven by the resolved pressoir.

## Capabilities

### New Capabilities

- `pressoir-abstraction`: a provider-neutral `Pressoir` interface + neutral domain model;
  GitLab and GitHub adapters; config-driven per-vignoble/per-repo provider selection;
  a single `aoc pressoir …` surface the extension calls instead of `glab`/`gh`; CI-status,
  approvals, comments, and planning-primitive mappings per provider.

### Modified Capabilities

- `aoc` (spawn / watch / track-mr / issue): pressoir calls go through `Pressoir`, not
  `glab` or the GitLab HTTP client directly. No user-visible behaviour change on GitLab.
- `conductor-issue-dispatch` / `mr-workflow` watchers: poll and merge through the seam;
  auto-merge gating maps pipeline (GitLab) or check runs (GitHub) to a neutral
  `CIStatus`.
- Conductor + worker extension tools (`comment_mr`, `track_mr`, issue/notes reads):
  call `aoc pressoir …` instead of `glab api …`.

## Impact

- **New package** `internal/pressoir` (interface + neutral model + GitLab adapter wrapping
  today's `internal/gitlab`; GitHub adapter added in a later phase).
- **Config** (`internal/config`): `pressoir`/`host`/`org`/`token_env` per vignoble & repo;
  `credentials.yaml` gains a GitHub identity alongside `gitlab`.
- **Extension**: remove direct `glab` calls from `pi-extension/{pinard,shared}`; route
  via `aoc pressoir`. Provider-aware prompt templates.
- **Release / GitOps**: the GitLab-CI release flow (`.gitlab-ci.yml`, `cz bump`,
  olympus-cd amend, `rules:changes` gating) has **no GitHub equivalent** — a GitHub
  Actions release path is a separate, later workstream, out of scope for the seam.
- **Docs**: pressoir configuration; GitHub onboarding; capability/permission differences.
- **Non-goals (this change)**: no bidirectional GitLab↔GitHub mirroring; no migration
  tooling; the Actions release rewrite is deferred and tracked separately.

## Decisions (resolved on the draft MR)

- **GitHub is the long-term *primary* git host; GitLab is secondary.** The neutral model
  is GitHub-shaped (pull request, `Number`, review, checks); GitLab is the adapter that
  maps its concepts in (`MR→PR`, `iid→Number`, `pipeline→checks`, `approvals→reviews`)
  and is not privileged. Phase 0 still starts from GitLab only because it is Pinard's
  current git host and the safe refactor baseline.
- **Auth = fine-grained PAT** (scope: repo + `contents`/`pull_requests`/`issues`/`checks`).
  GitHub App (installation tokens) is deferred to a documented future path for org/bot
  deployments. Caveat: GitHub branch protection cannot require a review from "someone
  other than the author" when a single PAT identity is both author and merger (no
  bot/owner split like the GitLab owner-token path); solo repo owners configure
  protection accordingly or admin-merge.
- **Epics → parent issue + native sub-issues** (closest hierarchy match). Task-list in
  the parent body is the fallback when sub-issues are unavailable. Projects v2 (board,
  not hierarchy) and milestones (flat) are rejected as the primary analog.
- **No webhooks** (they require a publicly reachable endpoint Pinard does not have).
  Polling stays; the GitHub adapter is rate-limit-aware (ETag conditional requests —
  a `304` does not count against the 5000/hr budget — plus GraphQL batching and backoff
  on `403 rate-limit`).
- **Prompt corpus is neutralized in Phase 0** (not deferred) so there is no
  half-GitLab / half-neutral intermediate state.

> This proposal defines the **shape and phasing**. The remaining concrete child-issue
> breakdown is left to discussion on the tracking MR (draft) before issues are filed.
