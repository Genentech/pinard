# Design: sandboxed-worker-bootstrap

## Context

pinard already runs sandboxed workers via `singularity run --containall <binds>
<sif>` — the launcher in `cmd/aoc/cmd_spawn.go` and the canonical
`dist/singularity/run-worker.sh`. `cmd/aoc/cmd_launch.go` provides
`ensure-proxy-provider` (seeds `~/.pi/agent/{models,auth}.json` from
`~/.claude/settings.json`, minting the token via its `apiKeyHelper`) and emits
`ENGRAM_CLOUD_*` etc. `internal/config/vignes.go` defines `runtime: singularity`
and `binds`. This change extends that machinery; it does not replace it.

Reference secret backend: **mnemosyne** (`https://mnemosyne.example.com`),
an *Actions* broker — a stored action is invoked via `GET /actions/{id}/do`,
returns JSON, and is revocable (`/revoke`, `410 Gone`) and rate-limited (`429`).
pinard treats the URLs as opaque HTTP endpoints; mnemosyne is the reference, not
a hard dependency.

## The two URLs

- **`PINARD_UNCORK_URL`** — one shared bottle, uncorked once → the shared
  credential/config bundle (`credentials.yaml` for NATS/engram/GitLab, plus any
  non-secret config). Infra-provisioned; the same for every operator.
- **`PINARD_POUR_URL`** — each operator pours their own glass → their LLM token,
  used as the proxy credential. Per-person; supports repeated pours (refresh).
  Off-boarding = revoke that operator's action; no image/shared-cred change.

The metaphor encodes the shared-vs-per-operator split.

## Bundle manifest

`PINARD_UNCORK_URL` returns JSON:

```json
{ "files": [ { "path": ".config/pinard/credentials.yaml", "mode": "0600", "content": "…" }, … ] }
```

- `path` is relative to `$HOME`; `mode` defaults to `0600`; `content` is the file
  body (base64 allowed via an optional `"encoding": "base64"`).
- A manifest (not a tarball) keeps it auditable, extraction-free, and explicit
  about modes.

## `aoc uncork`

```
aoc uncork [--url $PINARD_UNCORK_URL] [--home $HOME]
```

- Reads the manifest from the URL (or stdin), validates shape, writes each file
  under `$HOME` creating parent dirs, applies the mode, fails fast (non-zero) on
  non-2xx HTTP, `410`, or malformed JSON.
- Idempotent: re-running overwrites with the fetched content.

## `ensure-proxy-provider` without settings.json

Today it reads `apiKeyHelper` + `ANTHROPIC_BASE_URL`/headers from
`~/.claude/settings.json`. Target:

- Provider **registry** (`models.json`) is built from **image-baked defaults**
  (base URL, header names, model ids) — all non-secret.
- Provider **credential** (`auth.json`) is minted by calling `PINARD_POUR_URL`.
- `~/.claude/settings.json` becomes optional: if present it still works (back-compat);
  if absent, baked defaults + `PINARD_POUR_URL` suffice. This fixes the
  "No models available" failure for every `--containall` worker, not just one image.

## RAM-only secrets

- Launcher/`run-worker.sh` run with `--containall --writable-tmpfs` so the rootfs
  overlay is tmpfs (RAM). `aoc uncork` writes into `$HOME` there → secrets exist
  only in RAM and vanish on exit; nothing is written to host/shared disk.
- Persistent artifacts (worker run-state, run dirs) are written to explicitly
  bound persistent paths, not the tmpfs. Callers must ensure bulky writes target
  a bind, and size the tmpfs if `~/.pi` grows.

## Bootstrap ordering (`run-worker.sh`)

```sh
# singularity run --containall --writable-tmpfs <binds> <sif> <args>
if [ -n "${PINARD_UNCORK_URL:-}" ]; then aoc uncork --url "$PINARD_UNCORK_URL"; fi
aoc ensure-proxy-provider || true          # baked defaults + PINARD_POUR_URL
exec <image runscript / worker> "$@"
```

When `PINARD_UNCORK_URL` is unset, skip uncork and rely on bind-mounted creds
(current behavior) — this is the migration path.

## Binds

- The launcher skips `--bind` entries whose host source does not exist (warn, not
  FATAL). This is generic and independent of bootstrap.
- With bootstrap, a vigne's bind allow-list collapses to its **data**/work paths
  plus host-specific runtime deps (e.g. a SLURM client) — the credential binds
  (`~/.config/pinard`, `~/.claude/settings.json`, cert dirs) are no longer
  required. Vignes declare their remaining binds in vignes.yaml / their run.sh.

## Security

- URLs are bearer secrets → prefer short-TTL / one-time / signed actions; rely on
  the backend's revoke/`410` for rotation and off-boarding.
- Env vars are readable via `/proc` by the same OS user (single-tenant hosts →
  acceptable); `--env-file` or a bound file is available if stricter isolation is
  needed.
- Startup needs `curl` + network to the secret backend (already required for
  NATS / the current apiKeyHelper).

## Alternatives considered

- **Keep bind-mounting host creds** — fragile, per-host provisioning, secrets on
  disk. Kept only as the back-compat fallback.
- **Bake settings.json** — only safe if it holds no literal secret; since its
  sole secret is the token URL (an env var now), dropping the dependency is
  cleaner.
- **Tarball bundle** — a JSON manifest is more auditable and needs no extraction.
- **ExoSafe as broker** — separate system; the URL contract is provider-agnostic,
  mnemosyne is the reference backend.
