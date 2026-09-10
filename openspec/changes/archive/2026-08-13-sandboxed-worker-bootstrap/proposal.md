# Change: sandboxed-worker-bootstrap

## Why

Sandboxed pinard workers (`runtime: singularity`, `--containall`) currently get
their credentials by **bind-mounting host files at fixed paths** — the launcher
(`cmd_spawn.go`) and the generic `dist/singularity/run-worker.sh` mount
`~/.config/pinard`, `~/.claude/settings.json`, and per-vigne extras
(e.g. temporal certs). This is generic pinard behavior, and it has generic
problems:

- **Fragile:** singularity FATALs on a missing bind source, so every host must be
  pre-provisioned with every credential at the exact path.
- **Unscoped secrets:** credentials sit on the shared/host filesystem instead of
  being ephemeral.
- **"No models available":** with `--containall` the worker's `~/.pi` is empty;
  `ensure-proxy-provider` seeds it from `~/.claude/settings.json`, whose only
  secret is the `apiKeyHelper` token URL — so a whole config file is mounted just
  to carry one secret, and the seed step must be wired per image.

This capability generalizes the credential path for **any** sandboxed worker:
fetch secrets at startup from revocable URLs, keep them in RAM, and bind only
what a vigne genuinely needs. Vignes (e.g. example-build) then only declare their
data binds and bundle contents.

## What Changes

- **Bootstrap env contract (`PINARD_UNCORK_URL` / `PINARD_POUR_URL`):**
  - `PINARD_UNCORK_URL` — a revocable secret URL returning a JSON manifest of
    files to materialize (shared credentials such as `credentials.yaml` for
    NATS/engram/GitLab + non-secret config). "Uncork the shared bottle."
  - `PINARD_POUR_URL` — a revocable secret URL that mints the per-operator LLM
    token used as the proxy credential. "Each operator pours their own glass."
  Both are opaque to pinard (any HTTP endpoint returning the expected shape;
  mnemosyne `GET /actions/{id}/do` is the reference backend).
- **`aoc uncork`** — new subcommand: read the manifest from a URL (or stdin),
  write each file under `$HOME` with its declared mode (default `0600`), fail
  fast on non-2xx / malformed input.
- **`ensure-proxy-provider` from baked defaults + `PINARD_POUR_URL`:** build the
  pi proxy provider registry from image-baked non-secret defaults (base URL,
  headers, model ids) and mint the token from `PINARD_POUR_URL`, so
  `~/.claude/settings.json` is no longer required.
- **RAM-only secrets:** the singularity launcher (`cmd_spawn.go`) and
  `run-worker.sh` run with `--containall --writable-tmpfs`; bootstrapped files
  live only in the container's tmpfs home and never touch the host/shared disk.
- **Bootstrap ordering in `run-worker.sh`:** when `PINARD_UNCORK_URL` is set,
  `aoc uncork` → `ensure-proxy-provider` → the image's own runscript/worker.
  When unset, fall back to today's bind-mounted credentials (backward compatible).
- **Missing-bind resilience:** the launcher skips `--bind` entries whose host
  source is absent (with a warning) instead of letting singularity FATAL.

## Capabilities

### New Capabilities
- `sandboxed-worker-bootstrap`: how a `--containall` worker obtains runtime
  secrets — the uncork/pour URL contract, RAM-only materialization, proxy
  provider seeding from baked defaults, and the minimal-bind/missing-bind rules.

### Modified Capabilities
- `aoc`: adds the `uncork` subcommand and extends `ensure-proxy-provider` to
  source the provider from baked defaults + `PINARD_POUR_URL`.
- `remote-dispatch`: the singularity launcher adds `--writable-tmpfs` and passes
  the bootstrap env through to sandboxed workers.

## Impact

- Affected specs: `sandboxed-worker-bootstrap` (new), `aoc`, `remote-dispatch`
- Affected code: `cmd/aoc/cmd_launch.go` (`ensure-proxy-provider`, new `uncork`),
  `cmd/aoc/cmd_spawn.go` (launcher flags + env pass-through + missing-bind skip),
  `dist/singularity/run-worker.sh` (bootstrap ordering, `--writable-tmpfs`),
  `dist/singularity/pinard-base.def` (baked proxy defaults), `internal/config/vignes.go`
  (if binds/bootstrap are surfaced in vignes.yaml).
- Consumed by per-vigne images (e.g. example-workers `sif-self-bootstrap`), which
  keep only their data binds + bundle contents.
- Backward compatible: unset `PINARD_UNCORK_URL` ⇒ legacy bind-mounted creds.
