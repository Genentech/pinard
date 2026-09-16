---
title: GitHub Setup
weight: 63
group: Reference
---

This guide covers everything needed to configure Pinard with a GitHub-backed repository.
For the GitLab equivalent see `docs/gitlab-setup.md` in the repo root.

> **Wine vocabulary.** Pinard calls its git-host abstraction the **pressoir** (the press).
> You set `provider: github` in `vignes.yaml` to select the GitHub adapter; the
> underlying API calls are managed by Pinard — you never write `gh` commands manually.

## Create the bot account

1. Create a GitHub account for the Pinard service account (e.g. `pinard-bot`).
2. Add a recognisable avatar so it's identifiable in PR activity.
3. Add the account as a **collaborator** with **Write** role on each repository it will
   work on. Write grants: push branches, open and merge pull requests, comment, read
   pipelines. Admin is not required.

## Fine-grained Personal Access Token

Create a fine-grained PAT for the `pinard-bot` account:

1. Log in as `pinard-bot` → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**
2. **Resource owner:** the org or user that owns the repositories
3. **Repository access:** select the repositories Pinard will work on (or "All
   repositories" for a blanket grant)
4. **Permissions** — set exactly the following:

| Permission | Level |
|-----------|-------|
| Metadata | Read |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Commit statuses | Read |
| Actions | Read and write |
| Workflows | **Read and write** |

> **Workflows: Read and write is required** to push commits that add or modify files
> under `.github/workflows/`. Without it `git push` will be rejected by GitHub even
> though the Contents permission is granted.
>
> **Actions: Read and write** lets Pinard read workflow run status and re-trigger failed
> CI runs. If you do not need CI re-triggering, Read is sufficient.
>
> **Graceful degradation.** The adapter degrades gracefully when a permission is
> unavailable (e.g. branch-protection read returning 403) — it logs a warning and
> continues with reduced functionality rather than failing hard.

5. Set a reasonable expiry and save the token.
6. Export it on the machine running Pinard:

```bash
export PINARD_GITHUB_TOKEN="github_pat_xxxxx"
```

Add it to `~/.config/pinard/env` so the daemon picks it up on start.

## Git push authentication

Pinard authenticates `git push` over **HTTPS using the PAT as a credential** — no SSH
key is needed for GitHub-backed vignes. It configures git internally via:

```
url.<host>.insteadOf = <host>
Authorization: x-access-token:<PAT>
```

This means:

- No `~/.ssh` key setup is required for GitHub repos (unlike GitLab, which uses SSH by
  default).
- You may still set `ssh_key` in the `github:` credentials block if your workflow
  requires SSH, but it is optional.
- Commits will appear as authored by **you** (`GIT_AUTHOR_*` from your global git
  config) and committed by the **bot** (`GIT_COMMITTER_*` from `credentials.yaml`).
  To have commits link to the bot account on GitHub, set `git_email` to the bot's
  GitHub-provided noreply address (e.g. `pinard-bot@users.noreply.github.com`).

## Host handling

Set `host` to the **git hostname** — not the API URL. Pinard resolves the correct API
base automatically:

| `host` value | API base used |
|-------------|---------------|
| `github.com` (default) | `https://api.github.com` |
| `github.example.com` (GHE) | `https://github.example.com/api/v3` |

You never put `api.github.com` in your config.

## Credentials file

Add a `github:` block to `~/.config/pinard/credentials.yaml` alongside `gitlab:`:

```yaml
gitlab:
  host: gitlab.example.com
  user: pinard
  token_env: PINARD_GITLAB_TOKEN
  ssh_key: ~/.ssh/pinard_id_ed25519
  git_name: Pinard
  git_email: pinard@example.com

github:
  host: github.com                          # or your GHE hostname
  user: pinard-bot                          # GitHub username of the service account
  token_env: PINARD_GITHUB_TOKEN            # env var holding the fine-grained PAT
  git_name: Pinard
  git_email: pinard-bot@users.noreply.github.com

nats:
  url: wss://nats.example.com
  user: lelongs
  password_env: PINARD_NATS_PASSWORD
```

The `gitlab:` block can be omitted entirely if you have no GitLab vignes.

## Register a GitHub-backed vigne

In `vignes.yaml`, set `pressoir.provider: github` on any vigne that lives on GitHub:

```yaml
# vignes.yaml — mixed vignoble example
gitlab_host: gitlab.example.com
gitlab_group: mygroup

vignes:
  gl-service:                         # uses the default GitLab pressoir
    path: ~/gl-service
    repo: mygroup/gl-service

  gh-lib:                             # overrides to GitHub for this vigne
    path: ~/gh-lib
    repo: my-org/gh-lib               # owner/name on GitHub
    default_branch: main
    pressoir:
      provider: github
      org: my-org
```

Or set a vignoble-level default so all vignes use GitHub:

```yaml
# vignes.yaml — all-GitHub vignoble
pressoir:
  provider: github
  org: my-github-org

vignes:
  my-api:
    path: ~/my-api
    repo: my-github-org/my-api
    default_branch: main
```

See [Configuration — Pressoir](/docs/configuration/#pressoir--git-host) for the full
`PressoirConfig` reference and precedence rules.

## Auto-merge setup

GitHub's auto-merge feature requires a specific repo configuration. Pinard reads
approval state before merging and lets GitHub enforce branch protection at the API level.

### Recommended setup

1. **Enable "Allow auto-merge"** on the repository:
   Settings → General → Pull Requests → ✓ Allow auto-merge

2. **Gate on status checks** rather than (or in addition to) required reviews. Required
   status checks (CI, linters) can be satisfied without a human reviewer, and GitHub
   will merge automatically when they pass.

   Settings → Branches → Branch protection rules → `main`:
   - ✓ Require status checks to pass before merging
   - Add your CI check names (e.g. `build`, `test`)
   - ✓ Require branches to be up to date before merging

3. **Single-PAT self-approval caveat.** GitHub rejects a pull request approval when the
   reviewer is the same identity that opened the PR (`Can not approve your own pull
   request`). A bot that opens a PR with one PAT cannot approve that same PR with the
   same identity. Consequence: if the repo requires at least one human review, the bot
   cannot satisfy it alone.

   **Recommended:** gate auto-merge on **status checks** (step 2) rather than required
   approvals. If a human review gate is needed, provision a **separate reviewer
   identity** (a second PAT or a GitHub App install token) to post the approval.

Pinard reads branch-protection approval state via the adapter before attempting to
merge. If the protection endpoint is unreadable (e.g. 403 due to insufficient PAT
scope), `required_approving_review_count` degrades to 0 and Pinard falls through to the
`Merge` call — GitHub enforces the rule at the API level and returns an error if the
requirement is unmet.

## Capability and behavior differences vs GitLab

| Aspect | GitLab | GitHub |
|--------|--------|--------|
| **Merge requests** | Merge Request (`iid`) | Pull Request (`Number`) |
| **CI/CD** | Pipelines (`/pipelines`) | Check runs + workflow runs |
| **Approvals** | GitLab approval API; owner-token merge path | Branch protection / rulesets; no self-approve |
| **Webhooks** | Supported | **Polling only** — GitHub requires a public endpoint Pinard does not expose |
| **Rate limits** | No burst limit on self-hosted | 5 000 req/hr PAT budget; adapter uses ETag/304 conditional requests and GraphQL batching to stay within budget |
| **Planning (epics)** | Native GitLab Epics | Parent issue + native sub-issues; degrades to task-list in parent body when sub-issues unavailable |
| **Inline comments** | `position[...]` shape | Review comments (`path/line/commit_id`) |
| **Bot self-approve** | Possible via owner-token | Rejected by GitHub (`Can not approve your own pull request`) |

### No webhooks — polling only

GitHub webhooks require a publicly reachable endpoint. Pinard runs behind NAT and
does not expose one. The GitHub adapter polls for events using the standard Pinard
watcher contract, with the following efficiency measures to stay within the PAT
rate-limit budget:

- **ETag / `If-None-Match` conditional requests** — a `304 Not Modified` response does
  not count against the 5 000 req/hr limit.
- **GraphQL batching** — PR + checks + reviews in a single round-trip.
- **Backoff on `403 rate-limit`** — the adapter backs off and retries rather than
  hammering the API.

### Planning primitives

Group epics are a GitLab-only concept. On GitHub, Pinard maps an epic to a **parent
issue with native sub-issues** (a real parent→child hierarchy, automatable, org-scoped).
When sub-issues are unavailable the adapter degrades to a **task-list in the parent
issue body**. Projects v2 (a board, not a hierarchy) and milestones (flat) are not used
as the primary analog. The difference is surfaced via a `Capabilities()` probe so the
régisseur adapts its behaviour rather than failing silently.
