# Tasks: sandboxed-worker-bootstrap

## 1. `aoc uncork`
- [x] Add the `uncork` subcommand (`cmd/aoc/cmd_launch.go` or a new file): `--url`
      (default `$PINARD_UNCORK_URL`) / stdin, JSON manifest `{files:[{path,mode?,content,encoding?}]}`.
- [x] Write files under `$HOME` (relative paths), create parent dirs, apply mode
      (default `0600`), support `encoding: base64`.
- [x] Fail fast on non-2xx / `410` / malformed JSON; no partial-but-used writes.

## 2. `ensure-proxy-provider` from baked defaults + pour
- [x] Build `~/.pi/agent/models.json` from baked non-secret defaults (base URL,
      headers, model ids) instead of requiring `~/.claude/settings.json`.
- [x] Mint `~/.pi/agent/auth.json` token from `PINARD_POUR_URL`.
- [x] Keep the `settings.json` path as backward-compatible fallback; keep idempotency.

## 3. RAM-only secrets in the launcher
- [x] `cmd/aoc/cmd_spawn.go`: add `--writable-tmpfs` to the `singularity run` invocation.
- [x] Pass `PINARD_UNCORK_URL` / `PINARD_POUR_URL` through to the container env.
- [x] Skip `--bind` entries whose host source is missing (warn, not FATAL).

## 4. Bootstrap ordering in the generic helper
- [x] `dist/singularity/run-worker.sh`: when `PINARD_UNCORK_URL` set →
      `aoc uncork` → `aoc ensure-proxy-provider` → exec worker; else legacy path.
      (Implemented in the image `%runscript` at `dist/singularity/pinard-base.def:77-81`,
      the correct in-container location; `run-worker.sh` is the host-side `singularity run` wrapper.)

## 5. Baked defaults
- [x] `dist/singularity/pinard-base.def`: bake the non-secret proxy provider
      defaults consumed by `ensure-proxy-provider`.

## 6. Config surface (optional)
- [ ] `internal/config/vignes.go`: if bootstrap URLs / bind sets should be
      declarable per vigne in vignes.yaml, add the fields; else leave as env.

## 7. Verify + docs
- [ ] Sandboxed worker starts with only bootstrap URLs + data binds; no cred binds;
      NATS/engram/GitLab/LLM all work; secrets absent from host after exit.
- [ ] Legacy (no URLs) path still works.
- [ ] Update `docs/` (remote-workers) + `dist/singularity/` README.

## 8. Downstream
- [x] example-workers `sif-self-bootstrap` consumes this: keeps only its data binds,
      bundle contents, and `Example_ROOT` configurability.
