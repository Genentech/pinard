> Phasing is a **proposal for discussion** on the tracking MR (draft). Child issues are
> filed only after the phase breakdown is agreed. Blocking order:
> Phase 0 (seam) → Phase 1 (GitHub adapter) → Phase 2 (divergent surfaces).
> The GitHub Actions release path is a **separate deferred workstream**, not gated here.

## 0. Consolidate onto a pressoir seam (GitLab-only, no behaviour change)

- [ ] 0.1 Define `internal/pressoir`: `Pressoir` interface + neutral model (`PullRequest`
  with `Number`, `Issue`, `Comment`, `Review`, `InlineComment`, `CIStatus`, `RepoRef`,
  `Capabilities`).
- [ ] 0.2 GitLab adapter wrapping today's `internal/gitlab` (thin; preserves `api/v4` +
  `PRIVATE-TOKEN`, iid→Number translation, `position[...]` inline mapping).
- [ ] 0.3 Migrate Go call sites off direct `glab`/HTTP: `cmd/aoc/cmd_spawn.go`,
  `internal/watcher/issues.go`, `internal/watcher/mrs.go`, `cmd_track_mr.go`,
  `cmd_issue.go`, `cmd_watch.go`, `orphan_recovery.go`.
- [ ] 0.4 Add `aoc pressoir …` subcommands covering what the extension needs
  (issue read, notes read, user resolve, PR comment, PR track).
- [ ] 0.5 Migrate the extension off `glab`: `pi-extension/pinard/index.ts`,
  `pi-extension/shared/tools.ts` call `aoc pressoir …`. No `glab` in TS.
- [ ] 0.6 Config: `pressoir`/`host`/`org`/`token_env` fields (default `gitlab`), resolvable
  per vignoble and per repo; `credentials.yaml` unchanged for existing setups.
- [ ] 0.7 **Neutralize the prompt corpus** (pulled into Phase 0 for consistency): replace
  hard-coded `glab api …` / "MR" guidance in worker/babysitter/conductor prompts
  (`pi-extension/{pinard,worker,babysitter}`, `cmd/aoc/cmd_spawn.go`) with
  pressoir-resolved, provider-aware text. GitHub-shaped vocabulary is canonical
  (pull request / review / checks); GitLab wording is produced by the GitLab adapter.
- [ ] 0.8 Green on GitLab end-to-end (spawn → change request → CI gate → auto-merge →
  comments) with zero behaviour change. **Gate for Phase 1.**

## 1. GitHub adapter

- [ ] 1.1 GitHub adapter (REST + GraphQL): PR CRUD, list/get issues, comments, labels,
  user resolve; `owner/repo` ref; `Number` semantics.
- [ ] 1.2 Auth: **fine-grained PAT** (`Authorization: Bearer`; scope repo +
  `contents`/`pull_requests`/`issues`/`checks`); `credentials.yaml` GitHub identity.
  (GitHub App deferred — a shared App needs a central key-holding service Pinard does not
  have; a per-user App is more friction than a PAT. See design.md.)
- [ ] 1.3 CI mapping: aggregate check runs + required workflow runs → neutral
  `CIStatus`; wire auto-merge to it.
- [ ] 1.4 **Rate-limit-aware polling** (webhooks rejected — no public endpoint): ETag /
  `If-None-Match` conditional requests (304 ≠ quota), GraphQL batching of
  PR + checks + reviews, backoff on `403 rate-limit`. Watcher contract unchanged.
- [ ] 1.5 Config-driven selection verified: a GitHub-backed vignoble spawns, opens a PR,
  gates on checks, auto-merges.

## 2. Divergent surfaces

- [ ] 2.1 Approvals: GitHub PR review `APPROVE`; branch-protection/rulesets + `auto_merge`
  in place of the GitLab owner-token-merge path.
- [ ] 2.2 Inline review comments: neutral `InlineComment` → GitHub reviews API
  (`path/line/commit_id`).
- [ ] 2.3 Planning: epic ⇒ **parent issue + native sub-issues** (task-list-in-parent
  fallback); `Capabilities()` probe so the régisseur adapts instead of failing.
  Projects v2 / milestones rejected as the primary analog.

## 3. Docs

- [ ] 3.1 Pressoir configuration reference (provider, host, org, tokens; per-vignoble/repo).
- [ ] 3.2 GitHub onboarding guide + capability/permission differences vs GitLab.

## Deferred (separate workstream, not gated by this change)

- [ ] D.1 GitHub Actions release path equivalent to `cz bump` + olympus-cd GitOps amend
  + `rules:changes` gating. Tracked independently.
