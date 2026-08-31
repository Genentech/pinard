---
title: Distributed & Remote Execution
weight: 21
group: Building on Pinard
---

A vendangeur doesn't have to run on the Pinard host. Because every component talks only
over NATS, a worker can run on a **different machine that has no vignoble directory at
all** — for example an HPC node inside a Singularity sandbox — and still be driven by the
vignoble's conductor.

<figure class="doc-figure">
  <div class="doc-figure-visual">
    <img src="/images/docs/remote-worker-boundaries-v3.jpg" alt="A sketched Pinard estate connects over one outbound NATS route to a remote stone vineyard research cellar containing an isolated worker sandbox, secrets stored only in RAM, an external persistent journal, and a terminal responder antenna.">
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 12.1%; --y: 82.2%;">Pinard host</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 46.2%; --y: 46%;">→ · Outbound NATS</span>
    <span class="doc-figure-label doc-figure-label--desktop mustard" style="--x: 78%; --y: 75%;">K · Secrets stored in RAM</span>
    <span class="doc-figure-label doc-figure-label--desktop terracotta" style="--x: 92.5%; --y: 79%;">J · Run journal</span>
    <span class="doc-figure-label doc-figure-label--desktop charcoal" style="--x: 79%; --y: 10%;">▣ · Responder on isolated remote host</span>
  </div>
  <figcaption><strong>The network and persistence boundaries are different.</strong> Control travels over outbound NATS; resume state persists outside the sandbox; bootstrapped secrets exist only inside its RAM-backed home.</figcaption>
  <ul class="doc-figure-legend" aria-label="Remote worker boundaries">
    <li><span class="doc-figure-key">→</span><span><strong>NATS namespace</strong> identifies the vignoble without requiring its directory.</span></li>
    <li><span class="doc-figure-key charcoal">J</span><span><strong>Stable run ID + bound runs directory</strong> make restart and resume possible.</span></li>
    <li><span class="doc-figure-key terracotta">K</span><span><strong>Uncork/pour credentials</strong> are written into ephemeral RAM and vanish at stop.</span></li>
    <li><span class="doc-figure-key">▣</span><span><strong>Web-terminal responder</strong> provides observation without opening inbound SSH.</span></li>
  </ul>
</figure>

## Launch

Identify the vignoble by **name** (the NATS namespace) instead of a path:

```bash
pinard --worker --vignoble-name myproject --model <concrete-model-id> --args '<json>'
```

With `--vignoble-name`, the launcher skips all vignoble-directory resolution and
`$VIGNOBLE`-derived setup. Because model resolution normally needs the vignoble, a
standalone worker must pass a **concrete** `--model <id>`.

## Where config comes from

| Concern | Source |
|---------|--------|
| NATS credentials | `~/.config/pinard/credentials.yaml` (via `aoc env-exports`) — **not** the vignoble |
| NATS namespace | `--vignoble-name` |
| Babysitter process | Baked into the project repo at `pinard/<proc>/process.js` (override with `PINARD_PROCESS_FILE`) |
| Process inputs | `--args '<json>'`, or synthesized with the vignoble-free `aoc vigne-args --repo <group/proj> …` |

## Resuming after failure

Babysitter resumes a run from `<runs-dir>/<RUN_ID>/run.json`, so a remote worker needs
two things to survive a restart:

- a **stable `RUN_ID`** (defaults to `<project>-<process>`, not the PID-based session
  name), and
- a **persistent `--runs-dir`** on a bound path.

Re-running with the same `RUN_ID` continues where it left off. Note that environment
variables don't cross `singularity --containall`, so pass `--runs-dir` as an argument
(not an env var), pointing at a persistent, bound directory. Daemon-side orphan recovery
does not manage remote runs.

### Persistent pi session

When `BABYSITTER_RUNS_DIR` and `BABYSITTER_RUN_ID` are set (which `aoc spawn --runtime
singularity` does automatically), the launcher saves the worker's pi conversation log
under the run directory:

```
<BABYSITTER_RUNS_DIR>/<BABYSITTER_RUN_ID>/pi-session.jsonl
```

Because the run directory is on a **bound, persistent path**, the session transcript
survives container restarts and is visible off-host. A resumed run (same `RUN_ID`) also
continues the same pi conversation. The path is logged to stderr at startup. If the run
directory is unknown, the worker falls back to pi's default (`~/.pi/agent/`).

### Standalone engram

Standalone workers (`pinard --vignoble-name …`) have no daemon to own an engram serve.
The launcher **starts its own `engram serve`** on the computed port (derived from the
vignoble name: `cksum(name) % 1000 + 7500`) and sets `ENGRAM_URL` so the standard
health gate applies. If the serve is already healthy (e.g. a persistent container
reuse), the spawn is skipped. The `engram` binary must be available on `PATH` — the
launcher exits with a clear error if it is not.

Cloud sync follows the same periodic-flush loop as any other worker.

## Process file override

By default the launcher resolves the babysitter process definition from the project repo
(`pinard/<proc>/process.js`) or the vignoble. Set `PINARD_PROCESS_FILE` to an absolute
path of an existing file to **skip that search entirely** and use the given file instead:

```bash
export PINARD_PROCESS_FILE=/opt/pinard/processes/my-build.js
pinard --worker --vignoble-name myproject --process my-build …
```

This is useful when:
- The process is baked into a container image at a fixed path.
- You want to iterate on a local copy without touching the repo checkout.

If `PINARD_PROCESS_FILE` is unset or points at a missing file, the search falls through
to the normal lookup order. The resolved path (and whether the override was applied) is
logged to stderr on startup.

## Sandboxed bootstrap

When a worker runs with `--containall` in a Singularity sandbox, the container's `$HOME`
starts empty — there are no bind-mounted credential files. **Credential bootstrap** lets the
image obtain its secrets at startup from revocable URLs, so the image itself never contains
any credentials.

Two env vars drive the bootstrap, both passed in by `aoc spawn --runtime singularity` (or by
the operator directly) and consumed by the image's runscript (via `dist/singularity/run-worker.sh`):

| Env var | Purpose |
|---------|---------|
| `PINARD_UNCORK_URL` | URL of the shared credential bundle (NATS creds, `credentials.yaml`, …) |
| `PINARD_POUR_URL` | URL that mints a per-operator LLM token (the pi proxy credential) |

### What happens at startup

When `PINARD_UNCORK_URL` is set, the generic runscript does three things before the worker starts:

```
1. aoc uncork --url $PINARD_UNCORK_URL     # write shared creds under $HOME
2. aoc ensure-proxy-provider               # seed ~/.pi/agent/{models,auth}.json
3. exec pinard --worker …                  # start the worker
```

When `PINARD_UNCORK_URL` is **not** set, the legacy bind-mount path is used (step 3 only).

### RAM-only secrets

The container always runs with `--containall --writable-tmpfs`, so the ephemeral `$HOME`
lives entirely in RAM. Secrets written by `aoc uncork` never touch the host filesystem;
they vanish when the container stops.

### LLM token (pour)

`PINARD_POUR_URL` points at an endpoint that returns a short-lived LLM token (JSON or a bare
string). Two operators can share the same `PINARD_UNCORK_URL` but each supply their own
`PINARD_POUR_URL`, so revoking one URL disables only that operator's sessions.

The proxy provider is now registered directly from env vars baked into the container
image, **without** requiring `~/.claude/settings.json`:

| Env var | Purpose |
|---------|--------|
| `PINARD_PROXY_BASE_URL` | Anthropic proxy endpoint (baked into the image) |
| `PINARD_PROXY_HEADERS` | Extra HTTP headers for the proxy (e.g., `Auth-Type: bedrock`) |
| `PINARD_POUR_URL` | URL that returns a short-lived LLM token; read at startup |

These are read by the Pi extension directly from the process environment. Hosts that
already have `~/.claude/settings.json` continue to work unchanged (settings.json wins).

`aoc ensure-proxy-provider` (the explicit bootstrap step) remains available for images
that prefer to pre-seed `~/.pi/agent/{models,auth}.json` before the worker starts.

### Minting a bundle URL

Use the `mint-uncork-url` Pi skill (available to the conductor) to create or rotate a
bundle stored in Mnemosyne. The skill generates a time-limited signed URL and captures the
admin `/view` link for auditing.

### Missing bind sources

`aoc spawn --runtime singularity` now **skips** any `--bind` entry whose host source path
doesn't exist (warns instead of aborting), so optional bind mounts (e.g., cert directories
that only exist on some cluster nodes) can be declared in `vignes.yaml` without failing
the launch on nodes that lack them.

## Watching a remote worker

Run `aoc webterm-responder` on the remote host and use the [Web Terminal](/docs/web-terminal/)
to watch it from a browser — no SSH required.
