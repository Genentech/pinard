# GitHub provider — live gate runbook

This document describes how to verify that config-driven GitHub provider selection
works end to end: spawn → PR → checks → auto-merge.

## Prerequisites

- `aoc` rebuilt from this branch (`cd cmd/aoc && make install`)
- A GitHub PAT (fine-grained) with scopes: **repo contents**, **pull requests**,
  **issues**, **checks** — for the target org/repo.
- A test repository on GitHub (e.g. `sirloon/exohub-test`, default branch `main`).

## 1. Add credentials

In `~/.config/pinard/credentials.yaml`, add a `github:` block:

```yaml
github:
  host: github.com          # omit for github.com; set for GHE, e.g. ghe.corp.com
  user: pinard-bot          # GitHub username of the service account
  token_env: PINARD_GITHUB_TOKEN
  git_name: "Pinard Bot"
  git_email: pinard-bot@example.com
```

Export the PAT:

```bash
export PINARD_GITHUB_TOKEN=ghp_…
```

## 2. Add a vigne with `pressoir: github`

In `vignes.yaml`:

```yaml
vignes:
  exohub-test:
    path: ~/exohub-test
    repo: sirloon/exohub-test
    default_branch: main
    pressoir:
      provider: github
```

Clone the repo locally (the daemon uses this as the git root for worktrees):

```bash
git clone https://github.com/sirloon/exohub-test.git ~/exohub-test
```

## 3. Rebuild and restart the daemon

```bash
cd cmd/aoc && make install
aoc daemon restart
```

Confirm the daemon starts cleanly:

```bash
aoc daemon status
tail -20 logs/aoc-daemon.log
```

## 4. Open a test issue

Create an issue in `sirloon/exohub-test` on GitHub (via the web UI or `gh`):

```bash
gh issue create --repo sirloon/exohub-test \
  --title "Test pinard GitHub integration" \
  --body "Add a small README change to verify end-to-end flow."
```

Note the issue number (e.g. `#1`).

## 5. Spawn a worker

```bash
aoc spawn \
  --project exohub-test \
  --issue 1 \
  --prompt "Add a one-line change to README.md (append today's date) to verify GitHub integration."
```

Watch the worker session:

```bash
tmux -L pinard-<vignoble> attach -t <session-name>
```

## 6. Verify the PR flow

The worker should:

1. Create a branch and commit the change.
2. Push to GitHub (via HTTPS PAT — no SSH key needed).
3. Open a PR with `aoc pressoir open-pr --repo sirloon/exohub-test …`.
4. Call `aoc track-mr --repo sirloon/exohub-test --mr <number>` so the daemon watches it.

On the daemon side:

```bash
tail -f logs/aoc-daemon.log | grep exohub-test
```

You should see:
- `[mr-watcher] Tracking: MR !<n> (exohub-test, opened)`
- `[auto-merge] Attempting merge …` once CI passes and the PR is approved.

## 7. Verify auto-merge

If `auto_merge: true` is set on the vignoble or vigne, the daemon merges the PR
automatically once all required checks pass. Confirm:

```bash
gh pr view <number> --repo sirloon/exohub-test
# State should show: MERGED
```

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `git push` fails with 403 | PAT not set or missing `contents: write` scope |
| MR watcher uses wrong adapter | Daemon not restarted after adding pressoir config |
| GraphQL errors on GHE | Set `host: ghe.corp.com` (not `ghe.corp.com/api/v3`) — normalisation is automatic |
| CI never reaches "success" | Check runs not enabled on the repo; workflow may need a `push` trigger |
