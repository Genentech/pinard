---
title: Getting Started
weight: 2
group: Introduction
---

This guide takes you from nothing to a running vignoble with a conductor.

<figure class="doc-figure doc-figure--wide">
  <div class="doc-figure-visual">
    <img src="/images/docs/getting-started-journey-v2.jpg" alt="A six-stop sketched journey from a Pinard installation crate through credentials, estate creation, repository registration, daemon startup, and an open conductor control room.">
    <span class="doc-figure-label charcoal" style="--x: 8.3%; --y: 17.8%;">1</span>
    <span class="doc-figure-label charcoal" style="--x: 24.3%; --y: 17.8%;">2</span>
    <span class="doc-figure-label charcoal" style="--x: 41.5%; --y: 17.8%;">3</span>
    <span class="doc-figure-label charcoal" style="--x: 57.2%; --y: 17.8%;">4</span>
    <span class="doc-figure-label charcoal" style="--x: 73%; --y: 17.8%;">5</span>
    <span class="doc-figure-label charcoal" style="--x: 89.1%; --y: 17.8%;">6</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 8%; --y: 76%;">Install</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 24%; --y: 76%;">Credentials</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 41.5%; --y: 76%;">Create vignoble</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 57%; --y: 76%;">Register vigne</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 73%; --y: 76%;">Start daemon</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 90%; --y: 76%;">Launch conductor</span>
  </div>
  <figcaption><strong>Your first vignoble, in six stops.</strong> The illustration is a progress map; the commands below remain the source of truth.</figcaption>
  <ol class="doc-figure-legend doc-figure-legend--column-major" aria-label="Getting started sequence">
    <li><span class="doc-figure-key">1</span><span>Install Pinard from a release bundle or source.</span></li>
    <li><span class="doc-figure-key">2</span><span>Configure GitLab and NATS credentials.</span></li>
    <li><span class="doc-figure-key">3</span><span>Scaffold the vignoble with <code>aoc init</code>.</span></li>
    <li><span class="doc-figure-key">4</span><span>Register each repository as a vigne.</span></li>
    <li><span class="doc-figure-key">5</span><span>Start the always-on daemon.</span></li>
    <li><span class="doc-figure-key">6</span><span>Optionally enter the conductor control room.</span></li>
  </ol>
</figure>

## Install

Pinard ships two ways.

### From a release bundle (recommended)

A release is a single self-extracting `.run` archive (Linux/glibc x64). It bundles the
`aoc` binary, the launcher, the Pi extensions, and a vendored Node + Pi runtime — so the
host needs only the thin CLIs `tmux git glab fzf`, no Node/npm.

```bash
./pinard-linux-x64.run          # installs to ~/.pinard, symlinks aoc/pinard into ~/.local/bin
```

### From source

```bash
cd pinard/cmd/aoc && make install   # builds + installs aoc to ~/.local/bin
./install                            # sets up config templates, permissions, pre-commit hook
```

**Runtime requirements:** `./install` enforces **Node ≥ 22.19.0** and **Pi ≥ 0.80.6**.
If `nvm` is available it activates the pinned Node (22 LTS) from `.nvmrc` automatically;
otherwise install Node 22 manually before running `./install`. Node 22 LTS ships
native prebuilts for `better-sqlite3`, so no C++ compiler is needed on the host.

Set `PINARD_NODE=/path/to/node` to override which node binary is used when the nvm
default is too old; the launcher and daemon both respect this variable.

`aoc` is a static Go binary with zero runtime dependencies.

The **engram CLI** is installed (or upgraded/downgraded) to the **same version the
cluster runs** — pinned in `.engram-version` at the repo root. This keeps the local and
cloud engram in lockstep; a version drift can cause cloud sync to fail due to
mutation/chunk format mismatches. Re-running `./install` after a cluster upgrade will
update your local engram CLI automatically.

## Credentials

Pinard authenticates to GitLab (as a dedicated service account) and to NATS. All fields
must be set explicitly — there are no built-in defaults. Copy the bundled template:

```bash
cp credentials.example.yaml ~/.config/pinard/credentials.yaml
# then fill in your values
```

Minimal `~/.config/pinard/credentials.yaml`:

```yaml
gitlab:
  host: gitlab.example.com           # GitLab API hostname (no scheme)
  user: your-bot-user                # GitLab username of the service account
  token_env: PINARD_GITLAB_TOKEN     # env var holding the PAT
  ssh_key: ~/.ssh/pinard_id_ed25519
  git_name: Pinard
  git_email: bot@example.com

nats:
  url: wss://nats.example.com        # NATS JetStream WebSocket URL (required)
  user: your-nats-user
  password_env: PINARD_NATS_PASSWORD
```

Then export the secrets in your shell (or put them in `~/.config/pinard/env`, which the
daemon reads on start):

```bash
export PINARD_GITLAB_TOKEN="glpat-xxxxx"
export PINARD_NATS_PASSWORD="xxxxx"
```

See [Configuration](/docs/configuration/) for the full schema including optional blocks
(`engram:`, `webterm:`, and the [Buddy Capsule](/docs/capsules/) `PINARD_MNEMOSYNE_URL`).

## Create a vignoble

`aoc init` scaffolds a complete vignoble directory and starts the daemon:

```bash
aoc init myproject --gitlab-host gitlab.com --gitlab-group mygroup
cd ~/vignoble-myproject
```

This creates `vignes.yaml`, `schedules.yaml`, `PINARD.md`, the `.state/`, `logs/`,
`changes/`, and `parcelles/` directories, and the conductor permission files. It has no
systemd dependency — the daemon self-supervises.

## Register a vigne

Add each repository you want to orchestrate:

```bash
aoc add vigne my-api --path ~/my-api --repo mygroup/my-api
```

This appends an entry to `vignes.yaml`. Repeat for every repo. (Add `--auto-merge` only if
you want that vigne's MRs merged automatically — it's off by default; see
[Configuration](/docs/configuration/).)

## Run the daemon

The daemon is the always-on engine (MR/issue/schedule watchers, auto-spawn, dispatch):

```bash
aoc daemon start      # self-daemonizes, logs to logs/aoc-daemon.log, PID in .state/daemon.pid
aoc daemon status     # check it's alive
```

`aoc daemon start/stop/restart/status` manage the background process. It hot-reloads
itself when the `aoc` binary, `vignes.yaml`, or `schedules.yaml` change.

## Launch the conductor

The conductor (régisseur) is an interactive Pi session that receives events, spawns
agents, and lets you steer work conversationally:

```bash
cd ~/vignoble-myproject
pinard
```

Run `pinard` from anywhere (with no vignoble) to pick and attach to a running session via
`fzf`. The conductor is optional — the daemon does the mechanical work on its own — but it
gives you the LLM-powered control room.

## Next steps

- [Orchestration & Parcelles](/docs/orchestration/) — how the régisseur, maîtres, and
  vendangeurs divide work.
- [Issue Workflow](/docs/issue-workflow/) — drive work from GitLab issues.
- [CLI Reference](/docs/cli-reference/) — the full `aoc` and `pinard` surface.
