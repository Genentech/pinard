# Design — GitHub support via a pressoir-provider abstraction

## Context

GitLab coupling today, measured on the real tree (excludes `.worktrees/`):

- **Two access paths, no shared seam.** `internal/gitlab/client.go` (raw
  `api/v4` + `PRIVATE-TOKEN`, models in `types.go`) is used by the Go watchers and
  several `cmd/aoc` commands; but `cmd/aoc/cmd_spawn.go`, `internal/watcher/issues.go`,
  `internal/watcher/mrs.go`, and the extension (`pi-extension/pinard/index.ts`,
  `pi-extension/shared/tools.ts`) shell out to `glab` directly and bypass it.
- **Model leakage.** ~136 `iid` references, 29 `merge_requests`, 20 `/notes`,
  75 `pipelines`; the `Position` model encodes GitLab's inline-comment shape.
- **Prompt leakage.** Worker/babysitter/conductor prompts hard-code `glab api …`
  and MR terminology.

No `pressoir`/`provider` config switch exists (the extension's `registerProxyProvider`
is the LLM proxy, unrelated).

## Goals

**Guiding principle: GitHub is the long-term *primary* git host; GitLab is secondary.**
The neutral model is therefore **GitHub-shaped** (pull request, `Number`, review,
checks) and GitLab is the adapter that maps its concepts *in* (`MR→PR`, `iid→Number`,
`pipeline→checks`, `approvals→reviews`). GitLab must **not** be privileged in the
abstraction. Phase 0 nonetheless *starts* from GitLab only, because GitLab is Pinard's
current git host and the safe zero-behaviour-change refactor baseline — not because it
is primary.

1. One pressoir seam (`Pressoir`) that every pressoir interaction flows through — Go **and** extension.
2. Neutral, **GitHub-shaped** domain model (`Number`, not `iid`; `PullRequest`, `review`,
   `checks` — no GitLab quirk leakage).
3. GitLab behaviour preserved exactly when the seam ships (pure refactor first).
4. GitHub as a first-class adapter selected by config, per vignoble and per repo.
5. Divergent semantics (CI, approvals, inline comments, planning) mapped explicitly,
   not papered over.

## Non-goals

- GitLab↔GitHub mirroring or cross-pressoir sync.
- A GitHub Actions **release** pipeline (the `cz bump` / olympus-cd GitOps flow is a
  separate workstream; the seam does not depend on it).
- Migrating existing vignobles off GitLab.

## The interface (sketch, not final)

```go
package pressoir

type RepoRef struct { Host, Owner, Name string } // GitLab: Owner="group/sub"

type PullRequest struct {
    Number       int
    State        string   // open | merged | closed
    Title, Body  string
    Labels       []string
    SourceBranch string
    WebURL       string
    Author       string
    MergeSHA     string
}

type CIStatus struct {
    State   string // success | failed | running | pending | none
    WebURL  string
}

type Pressoir interface {
    OpenPR(ctx, repo RepoRef, src, dst, title, body string, draft bool) (PullRequest, error)
    ListOpenPRs(ctx, repo RepoRef) ([]PullRequest, error)
    GetPR(ctx, repo RepoRef, number int) (PullRequest, error)
    Merge(ctx, repo RepoRef, number int, opts MergeOpts) error
    Approve(ctx, repo RepoRef, number int) error
    Comment(ctx, repo RepoRef, number int, body string) error
    InlineComment(ctx, repo RepoRef, number int, c InlineComment) error
    CIStatusFor(ctx, repo RepoRef, number int) (CIStatus, error)

    ListIssues(ctx, repo RepoRef, filter IssueFilter) ([]Issue, error)
    GetIssue(ctx, repo RepoRef, number int) (Issue, error)
    SetLabels(ctx, repo RepoRef, number int, labels []string) error
    ResolveUser(ctx, username string) (UserID, error)
}
```

The GitLab adapter wraps today's `internal/gitlab` (thin). The GitHub adapter uses
GitHub REST + GraphQL (GraphQL for efficient PR+checks reads under rate limits).

## Key mapping decisions

- **iid → Number.** The neutral model exposes a single `Number`. GitLab's separate
  issue/MR `iid` namespaces are an adapter detail; GitHub's shared numbering is fine
  because callers always know whether they hold a PR or an Issue.
- **CI gate.** Neutral `CIStatus{state}`. GitLab adapter derives it from pipeline
  status / `detailed_merge_status`; GitHub adapter aggregates **check runs + required
  workflow runs** into one state. Auto-merge logic keys on the neutral state only.
- **Approvals.** GitLab `approve` endpoint vs GitHub PR review `APPROVE`. The
  owner-token-merge-to-protected-branch trick (GitLab, obs #396) is adapter-internal;
  GitHub uses branch-protection/rulesets + `auto_merge`.
- **Inline comments.** Neutral `InlineComment{path,line,side}`; adapters translate to
  GitLab `position[...]` or GitHub reviews `path/line/commit_id`.
- **Planning primitives.** Group **epics** are GitLab-only. On GitHub an epic maps to a
  **parent issue with native sub-issues** (the closest structural analog: a real
  parent→child hierarchy, automatable, org-scoped). When sub-issues are unavailable the
  adapter degrades to a **task-list in the parent issue body**. Projects v2 (a board,
  not a hierarchy; GraphQL-heavy) and milestones (flat) are rejected as the primary
  analog. The difference is surfaced via a `Capabilities()` probe so the régisseur
  adapts rather than fails.
- **Auth = fine-grained PAT.** `credentials.yaml` gains a GitHub identity; the adapter
  sets `Authorization: Bearer` with a fine-grained PAT (repo + `contents` /
  `pull_requests` / `issues` / `checks`). GitLab keeps `PRIVATE-TOKEN`. A **GitHub App**
  (installation token, distinct bot identity, per-repo install) is the better answer for
  org/bot deployments but is **deferred**. Decisive reason it does not fit local Pinard:
  authenticating as an App requires the **App private key**. A single shared public
  "Pinard" App would mean either shipping that key to every user (a total non-starter —
  the holder can act as the App across *all* installations) or running a central,
  publicly-reachable token-minting service Pinard does not have (same constraint that
  killed webhooks). So a shared App is not viable; a **per-user App** (each user holds
  their own key) is safe but strictly more setup than a PAT, for no benefit in the solo
  case. An App only wins with a central operator (hosted Pinard) or an org managing its
  own App. A random user can create a fine-grained PAT in seconds — that is the priority.
  Caveat: with a single PAT identity as both author and merger,
  GitHub branch protection cannot enforce "review from someone other than the author"
  (no bot/owner split like the GitLab owner-token path, obs #396); solo owners configure
  protection accordingly or admin-merge.
- **Single-PAT self-approval caveat.** GitHub rejects `POST /pulls/{n}/reviews`
  with `APPROVE` when the reviewer is the PR author (`Unprocessable Entity: Can not
  approve your own pull request`). A bot that opens a PR with one PAT cannot approve
  that same PR with the same identity. Consequence: if the repo requires at least one
  human review (branch protection `required_approving_review_count ≥ 1`), the bot
  cannot satisfy it alone.

  **Recommended setup for Pinard auto-merge on GitHub:**
  1. Enable "Allow auto-merge" on the repo (Settings → General).
  2. Gate on **status checks** rather than (or in addition to) required reviews —
     checks (CI, linters) can be required without a human reviewer, and GitHub will
     merge automatically when they pass.
  3. If a human review gate is required, provision a **separate reviewer identity**
     (a second PAT belonging to a reviewer account, or a GitHub App with its own
     install token) to post the `APPROVE`. A single-PAT deployment cannot self-approve;
     branch protection will enforce the policy at `Merge` time and return an error if
     the requirement is unmet.

  The Pinard seam reads approval state via `GetApprovalStatus` before attempting
  `Merge`; if the protection endpoint is unreadable (403 / insufficient PAT scope)
  `Required` degrades to 0 and Pinard falls through to `Merge`, letting GitHub
  enforce the rule at the API level.

- **Change detection.** Polling only — GitHub **webhooks are not viable** (they require a
  publicly reachable endpoint Pinard does not expose). The GitHub adapter is
  **rate-limit-aware**: ETag/`If-None-Match` conditional requests (a `304` does not
  count against the 5000/hr PAT budget), GraphQL batching (PR + checks + reviews in one
  round-trip), and backoff on `403 rate-limit`. The watcher contract is unchanged.

## Extension consolidation (load-bearing)

The extension must **stop calling `glab` directly**. Instead of adding a parallel `gh`
path in TypeScript, the extension calls a thin `aoc pressoir …` subcommand (Go), so
both the CLI and the extension share the single `Pressoir` implementation. This collapses
the provider branching to one place and keeps the TS side provider-agnostic.

## Phasing (for discussion on the MR — not yet issues)

- **Phase 0 — consolidate (GitLab-only, no behaviour change).** Define `Pressoir` +
  neutral model; wrap `internal/gitlab`; migrate every Go call site and the extension's
  `glab` calls behind `aoc pressoir`. Ship green with only GitLab.
- **Phase 1 — GitHub adapter.** REST/GraphQL adapter; config-driven selection; map
  iid→number, MR→PR, notes→comments, pipeline→checks; GitHub App auth.
- **Phase 2 — divergent surfaces.** Approvals/branch-protection; inline reviews; epics
  via parent-issue + sub-issues + `Capabilities()`.

  (Provider-aware **prompt templates** are pulled forward into Phase 0 for consistency
  — see below. Webhooks are rejected; rate-limit-aware polling is part of the Phase 1
  adapter.)
- **Deferred workstream — release on GitHub.** Actions equivalent of the `cz bump` /
  olympus-cd GitOps flow. Independent of the seam.

## Risks

- **CI semantics** diverge the most; the neutral `CIStatus` must not lose the nuance
  auto-merge relies on (e.g. "mergeable but checks pending" vs "failed").
- **Prompt corpus** is large and behavioural; provider-aware templating is as much
  effort as the Go and easy to under-scope.
- **Rate limits** on GitHub (5000/hr PAT) make naive polling across many repos costly;
  GraphQL/webhooks may become necessary sooner than expected.
- **Epics gap** — the régisseur's epic-promotion behaviour needs a real fallback, not a
  silent no-op.
