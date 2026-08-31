---
title: CLI Reference
weight: 60
group: Reference
---

Two entry points: **`aoc`** (the Go binary — scaffolding, daemon, spawning, everything
mechanical) and **`pinard`** (the launcher shell script — starts the conductor tiers and
workers).

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/cli-command-map-v2.jpg" alt="A cellar tool board with six independent command-family stations: launcher, estate setup, runtime, observation, remote and funding, and administration. The stations are categories, not sequential steps.">
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 18%; --y: 43%;"><strong>L · Launcher</strong><br><code>pinard</code> · <code>--maitre</code> · <code>--worker</code></span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 50%; --y: 43%;"><strong>E · Estate setup</strong><br><code>init</code> · <code>add vigne</code> · <code>config</code></span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 83.33%; --y: 43%;"><strong>R · Runtime</strong><br><code>daemon</code> · <code>spawn</code> · <code>maitre</code></span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 16.67%; --y: 91%;"><strong>O · Observe</strong><br><code>status</code> · <code>dashboard</code> · <code>track-mr</code></span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 50%; --y: 91%;"><strong>X · Remote & funding</strong><br><code>uncork</code> · <code>webterm</code> · <code>capsule-*</code></span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 83.33%; --y: 91%;"><strong>A · Administration</strong><br><code>create-user</code> · <code>cleanup</code> · internals</span>
  </div>
  <figcaption><strong>Choose a family, then scan its reference section.</strong> These are independent tool groups, not a command sequence; the detailed syntax below remains authoritative.</figcaption>
  <ul class="doc-figure-legend" aria-label="CLI command families">
    <li><span class="doc-figure-key charcoal">L</span><span><strong>Launcher</strong> — enter a control tier, attach to a session, or start a worker with <code>pinard</code>.</span></li>
    <li><span class="doc-figure-key">E</span><span><strong>Estate setup</strong> — create the vignoble and register repositories, schedules, and configuration.</span></li>
    <li><span class="doc-figure-key terracotta">R</span><span><strong>Runtime</strong> — operate the daemon, agents, maîtres, and notifications.</span></li>
    <li><span class="doc-figure-key charcoal">O</span><span><strong>Observe</strong> — inspect status, dashboards, merge requests, schedules, and browser terminals.</span></li>
    <li><span class="doc-figure-key">X</span><span><strong>Remote & funding</strong> — bootstrap isolated hosts and manage optional Capsule Protocol commands.</span></li>
    <li><span class="doc-figure-key terracotta">A</span><span><strong>Administration</strong> — provision accounts, archive completed work, and support launcher internals.</span></li>
  </ul>
</figure>

## `pinard` (launcher)

```bash
pinard                              # in a vignoble dir: start the régisseur
pinard                              # anywhere else: fzf-pick a running session and attach
pinard --maitre <parcelle>         # start/attach a parcelle maître
pinard --worker …                  # run as a worker (see Remote Workers)
pinard --restart                   # kill and restart pinard tmux sessions
```

The launcher resolves its runtime as **bundled > nvm > PATH**, so a release bundle needs
no system Node.

## `aoc` — setup

### `aoc init [name]`

Scaffold a vignoble and start its daemon.

```bash
aoc init myproject --gitlab-host gitlab.com --gitlab-group mygroup [--path ~/vignoble-myproject]
```

### `aoc add vigne <name>`

Register a repository in the current vignoble's `vignes.yaml`.

```bash
aoc add vigne my-api --path ~/my-api --repo mygroup/my-api [--auto-merge]
```

### `aoc add schedule <name>` / `aoc schedule`

Add a cron-scheduled spawn (see [Scheduling](/docs/scheduling/)).

```bash
aoc add schedule nightly --project my-api --cron "0 2 * * *" --prompt "…"
```

### `aoc config get|set <path> [value]`

Read or write `vignes.yaml` with dot-path notation.

```bash
aoc config set vignes.my-api.auto_merge true
aoc config get models.worker.id
```

## `aoc` — daemon

```bash
aoc daemon start        # start detached (self-daemonizing, PID in .state/daemon.pid)
aoc daemon status       # liveness
aoc daemon stop
aoc daemon restart
```

`aoc daemon` (no subcommand) runs the watchers in the foreground. The one-shot compat
commands `aoc watch-mrs`, `aoc watch-issues`, and `aoc run-schedules` run a single cycle —
prefer the daemon for continuous operation.

## `aoc` — agents

### `aoc spawn`

Launch a worker in its own git worktree + tmux session.

```bash
aoc spawn --project my-api --prompt "Fix the auth bug in login.go"
aoc spawn --project my-api --prompt "Task" --target-branch cuvee/batch-1
aoc spawn --project my-api --issue 42 --parcelle semantic-search
```

| Flag | Purpose |
|------|---------|
| `--project` | Vigne/project name |
| `--prompt` | Task prompt |
| `--issue` | GitLab issue IID driving the work |
| `--parcelle` | Workstream name (defaults to the project) |
| `--target-branch` | MR target branch (auto-detected from repo default branch if omitted; `cuvee/<name>` branches are auto-created on origin if missing) |
| `--name` | Session name (auto-generated if omitted) |
| `--process` | Babysitter process definition |
| `--run-id` | Resume an existing babysitter run |
| `--runtime` | `local` (default) or `singularity` |
| `--sif` | Singularity image path (with `--runtime singularity`) |
| `--no-worktree` | Run in the project path without a worktree (data/orchestration jobs) |
| `--force` | Spawn even if a live worker already exists for this run ID |
| `--contract-id` | Mnemosyne contract ID — injects `PINARD_CAPSULE_CONTRACT` into the worker env; auto-detected from the issue when `--issue` is given |
| `--no-capsule` | Skip capsule auto-detection; spawn on operator token even if the issue has a funded contract |

### `aoc attach <session>`

Stream a vendangeur's terminal output over NATS to your **local terminal** — read-only,
no browser or web gateway required. Resolves the session from the `pinard-agents` KV by
name, agentId, or runId.

```bash
aoc attach my-session             # stream by session name
aoc attach abc123def              # stream by agentId or runId
aoc attach my-session --timeout 5m  # detach after 5 minutes idle
```

| Flag | Purpose |
|------|--------|
| `--vignoble-name` | Vignoble NATS namespace; defaults to `NATS_VIGNOBLE` |
| `--timeout` | Detach after this much idle time (0 = no timeout, the default) |

For sessions on the **local tmux host**, `aoc attach` also starts an in-process PTY
pump so output is available over NATS without a separate `aoc webterm-responder`
process. Press `Ctrl+C` to detach.

For a browser-based view, see `aoc webterm-link` and [Web Terminal](/docs/web-terminal/).

### `aoc maitre spawn|attach|list`

Manage per-parcelle maître windows.

```bash
aoc maitre attach --parcelle semantic-search   # spawn-if-missing, then switch to its window
aoc maitre list                                 # windows in the conductor session
```

### `aoc notify <message>`

Publish a notification to the conductor over NATS.

```bash
aoc notify "Task complete, opened MR !42"
```

## `aoc` — merge requests

```bash
aoc track-mr --session <s> --mr <n> --project <p>   # register an MR with the watcher
aoc untrack-mr --session <s>                          # stop watching
```

## `aoc` — status & schedules

```bash
aoc status              # tracked MRs, issues, workers, schedules
aoc dashboard           # live TUI: workers, MRs, schedules, notifications
aoc list-schedules      # schedules and their last run times
aoc unschedule --name <name>
```

## `aoc` — web terminal

```bash
aoc webterm-link --target <session>            # print a read-only browser link
aoc webterm-link --target <session> --auto     # same, but print nothing (exit 0) when webterm/post_links is off
aoc webterm-responder                          # run the host responder (standalone/HPC hosts)
```

The link is **unsigned** when Cognito SSO is enabled (gateway grants only SSO'd operators)
or **signed + expiring** otherwise. `--auto` is intended for automated callers that want
to append a link only when one exists.

See [Web Terminal](/docs/web-terminal/).

## `aoc uncork`

Materialize a credential/config bundle for a sandboxed or HPC worker. Reads a JSON
manifest from a URL or stdin and writes each listed file under `$HOME`.

```bash
aoc uncork                          # read manifest from stdin
aoc uncork --url <endpoint>         # fetch manifest from URL (default: $PINARD_UNCORK_URL)
aoc uncork --url <endpoint> --home /custom/home  # write files under a different base dir
```

The manifest is a JSON object:

```json
{
  "files": [
    { "path": ".config/pinard/credentials.yaml", "content": "…", "mode": "0600" },
    { "path": "encoded.bin", "content": "<base64>", "encoding": "base64", "checksum": "sha256:<hex>" }
  ]
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `path` | ✓ | — | Relative to `$HOME`; absolute paths and `..` traversal are rejected |
| `content` | ✓ | — | File body (plain string or base64-encoded) |
| `encoding` | — | `plain` | `base64` decodes the content field |
| `mode` | — | `0600` | Octal file permission string |
| `checksum` | — | none | Optional `sha256:<hex>` for integrity verification |

The command fails fast on any non-2xx response, a `410 Gone` (revoked bundle), or malformed JSON.
See [Remote Workers — Sandboxed bootstrap](/docs/remote-workers/#sandboxed-bootstrap) for the full workflow.

## `aoc` — capsules

Buddy Capsules fund a vendangeur's LLM quota through Mnemosyne.
See [Buddy Capsules](/docs/capsules/) for the full workflow.

### `aoc capsule-keygen`

Generate the ed25519 identity keypair for this Pinard host (one-time setup).

```bash
aoc capsule-keygen            # writes ~/.config/pinard/capsule_key.pem
aoc capsule-keygen --force    # rotate: overwrite existing keypair
```

### `aoc capsule-pubkey`

Print the base64-encoded raw ed25519 public key to share with funders.

```bash
aoc capsule-pubkey
```

### `aoc capsule-contract`

Create a Mnemosyne ContractAction and post the `contract_id` as a comment on a GitLab issue.

```bash
aoc capsule-contract \
  --title "Short label (≤60 chars)" \
  --description "What work is requested" \
  --repo mygroup/myproject \
  --issue 42
```

Authentication via device-auth flow (once); tokens cached at
`~/.config/pinard/mnemosyne-tokens.json`.

### `aoc capsule-redeem`

Redeem a funded Mnemosyne contract and print a bare Claude API token to stdout.
Called automatically by the worker startup path when `PINARD_CAPSULE_CONTRACT` is set;
you normally don't call it directly.

```bash
aoc capsule-redeem <contract_id>
```

### `aoc capsule-post-result`

Render `<rundir>/capsule-report.md` to HTML and PATCH the contract's result URL.
Called by the babysitter at the end of a funded run.

## `aoc` — admin & internals

```bash
aoc create-user --name alice --vignoble myproject     # NATS account/user (requires nsc)
aoc cleanup archive --project <p> --change <name>     # archive a completed openspec change
```

Additional internal subcommands (`resolve-model`, `vigne-args`, `env-exports`,
`ensure-proxy-provider`, `governance-prompt`, `nats-publish`) exist for the launcher and
daemon; you won't normally call them directly.

`aoc governance-prompt --process <name> [--host <gitlab-host>]` prints the no-op
bootstrap prompt used for process workers. The launcher calls it automatically when
no explicit prompt is given; expose it here so custom launch scripts can stay in sync
without hard-coding the text.
