# GitHub Setup for Pinard

## Create the `pinard-bot` account

1. Create a GitHub account named `pinard-bot` (or any service-account name)
2. Add it as a **collaborator** with **Write** role on each project it will work on

## Fine-grained Personal Access Token

Create a fine-grained PAT for the `pinard-bot` account:

1. Log in as `pinard-bot` → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token
2. Resource owner: the org/user that owns the repositories
3. Repository access: the repos Pinard will work on
4. Permissions:

| Permission | Level |
|-----------|-------|
| Metadata | Read |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Commit statuses | Read |
| Actions | Read and write |
| Workflows | **Read and write** (required to push `.github/workflows/` files) |

Save the token and set it on the machine running Pinard:

```bash
export PINARD_GITHUB_TOKEN="github_pat_xxxxx"
```

## Git push authentication

Pinard uses the PAT as `x-access-token:<PAT>` over HTTPS for `git push`. No SSH key
is needed for GitHub-backed vignes.

Set `git_email` to the bot's GitHub noreply address so commits link to the account:
`<username>@users.noreply.github.com`

## Credentials file

Add a `github:` block alongside `gitlab:` in `~/.config/pinard/credentials.yaml`:

```yaml
github:
  host: github.com                          # or GHE hostname (e.g. github.example.com)
  user: pinard-bot
  token_env: PINARD_GITHUB_TOKEN
  git_name: Pinard
  git_email: pinard-bot@users.noreply.github.com
```

## Branch protection (auto-merge)

For auto-merge to work:

1. **Enable "Allow auto-merge"** on the repo (Settings → General)
2. **Require status checks** rather than required approvals — checks can be satisfied
   without a human reviewer
3. **Single-PAT caveat**: GitHub rejects approving your own PR. A solo-PAT bot cannot
   satisfy a "required human review" rule. Use required status checks instead, or a
   second reviewer identity.

## What Pinard does with these permissions

| Action | API call | Required |
|--------|----------|----------|
| Push branch | `git push` via HTTPS PAT | Contents: write |
| Open PR | `POST /repos/{owner}/{repo}/pulls` | Pull requests: write |
| Merge PR | `PUT /repos/{owner}/{repo}/pulls/{n}/merge` | Pull requests: write |
| Comment on PR | `POST /repos/{owner}/{repo}/issues/{n}/comments` | Issues: write |
| Read CI status | `GET /repos/{owner}/{repo}/commits/{sha}/check-runs` | Commit statuses: read |
| Re-run workflow | `POST /repos/{owner}/{repo}/actions/runs/{id}/rerun` | Actions: write |
| Push workflow file | `git push` touching `.github/workflows/` | Workflows: write |

## Releasing on GitHub

The `.github/workflows/release.yml` workflow provides a GitHub-native release
path. The GitLab pipeline has **two separate version lineages** — an image
lineage (`image-$version` tags, bumped on code/infra path changes) and a chart
lineage (`v$version` tags, bumped on `charts/**` changes and consumed by Flux
CD). A standalone GitHub repo ships neither Helm charts nor Flux, so this
workflow **collapses both into a single `v*` release**: it runs `cz bump --yes`
on the root `pyproject.toml` (chart lineage config) and cuts a release tag
whenever code or infrastructure paths change — the same paths that trigger
`.bump-image` on GitLab.

### What the workflow does

1. **`cz bump --yes`** — commitizen reads conventional commits since the last
   `v*` tag, bumps the semver in `pyproject.toml` (`version_provider = pep621`),
   updates `CHANGELOG.md`, commits, and creates an annotated `v<version>` tag.
2. **Push** — the bump commit and tag are pushed back to `main`.
3. **GitHub Release** — a release is created from the new tag with the
   `CHANGELOG.md` entry for that version as the release body.

### Required permissions

The workflow uses `permissions: contents: write` so the default `GITHUB_TOKEN`
can push commits and create releases on an **unprotected** `main` branch.

For **protected branches** (push rules / branch protection), `GITHUB_TOKEN`
cannot bypass protection rules. You have two options:

- Add `github-actions[bot]` to the branch-protection bypass list (Settings →
  Branches → Edit rule → Allow specified actors to bypass).
- Create a **PAT** with `repo` scope for an account that has bypass rights,
  store it as the repository secret `RELEASE_TOKEN`, and the workflow will
  prefer it over `GITHUB_TOKEN` automatically.

### Tag protection

If you use GitHub's tag protection rules (Settings → Tags), ensure the `v*`
pattern allows pushes from `github-actions[bot]` or the PAT identity used above.

### olympus-cd gap

The GitLab flow includes an `olympus-cd` stage that amends the app
`GitRepository` watched by Flux CD, triggering a Kubernetes rollout. This step
depends on internal infrastructure (olympus, Flux, cluster credentials) that is
not reachable from GitHub Actions.

The workflow contains a clearly marked `TODO` stub in place of this step. To
wire it up for a self-hosted deployment:

1. Expose a webhook or API endpoint reachable from the Actions runner.
2. Store the endpoint URL and auth token as repository secrets.
3. Replace the stub comment with a `curl` (or `gh`) call to that endpoint.

Until then, the release commit, tag, and GitHub Release are all created
correctly — only the downstream GitOps trigger is absent.

### Manual steps and caveats

- **No manual trigger needed** — the workflow fires automatically on qualifying
  pushes. Use `workflow_dispatch` (add it to the `on:` block) if you ever need
  to cut a release manually.
- **Concurrency guard** — `cancel-in-progress: false` ensures a concurrent push
  never interrupts a `cz bump` mid-flight and leaves the repo in an inconsistent
  state. If a run is queued, it will start after the current one finishes.
- **No-op when there are no releasable commits** — if `cz bump` finds no
  conventional commits that warrant a version change (e.g. a `chore:` push)
  it exits non-zero and the workflow fails. This is intentional: treat it as a
  signal that the triggering commit did not need a release. You can suppress
  this by adding `|| true` after `cz bump --yes`, but then spurious runs
  produce no release either way.
- **Can only be verified on a live GitHub repo** — the push-back and release
  creation steps require real GitHub API access and cannot be fully exercised
  in a local environment or with `actionlint` alone.

## Key differences vs GitLab

- **No webhooks** — GitHub requires a public endpoint; Pinard polls instead
- **No self-approve** — GitHub rejects approving your own PR
- **5 000 req/hr rate limit** — adapter uses ETag/304 and GraphQL batching
- **Epics** → parent issue + sub-issues (task-list fallback)
