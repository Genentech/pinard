#!/usr/bin/env bash
# Shared dependency bootstrap for pinard — the SINGLE source of truth for turning
# a clean checkout into a runnable tree. Called by both `./install` (host/dev) and
# `dist/build.sh` (the `make dist` artifact) so the two can never drift.
#
# Does, idempotently (skips work already done; pass --force to redo):
#   1. init the deps/babysitter git submodule
#   2. select a C++20-capable compiler (better-sqlite3 compiles from source on
#      node majors without a prebuilt, e.g. node 24)
#   3. npm install deps/babysitter
#   4. BUILD the babysitter SDK (tsc → packages/sdk/dist) — no prepare hook, so
#      this must be explicit; both callers previously relied on a pre-built tree
#   5. create node_modules/@a5c-ai/babysitter-sdk → deps/babysitter/packages/sdk
#      so pinard-hosted processes/*.js can `import '@a5c-ai/babysitter-sdk'`
#   6. npm install the pi extensions (., pinard, worker)
#
# Usage: scripts/bootstrap-deps.sh [REPO_DIR] [--force]
set -euo pipefail

FORCE=0
REPO=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) REPO="$arg" ;;
  esac
done
REPO="${REPO:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"
cd "$REPO"

log() { printf '  %s\n' "$*"; }

# ── 1. babysitter submodule ────────────────────────────────────────────────
if [[ ! -f "$REPO/deps/babysitter/package.json" ]]; then
  log "- init babysitter submodule"
  git -C "$REPO" submodule update --init deps/babysitter
fi

# ── 2. C++20 compiler selection (for better-sqlite3 source builds) ──────────
# node majors without a shipped better-sqlite3 prebuilt (e.g. 24 / ABI 137)
# compile it via node-gyp, which needs -std=c++20. Old system g++ (<=9) only
# knows -std=c++2a and fails. Pick a working C++20 toolchain if the default
# can't; set CC+CXX from the same gcc family to keep the C/C++ objects ABI-consistent.
cxx_ok() { "$1" -std=c++20 -x c++ -E - </dev/null >/dev/null 2>&1; }
if [[ -z "${CXX:-}" ]] || ! cxx_ok "${CXX:-c++}"; then
  for cand in g++-16 g++-15 g++-14 g++-13 g++-12 g++-11 g++-10 \
              /home/linuxbrew/.linuxbrew/bin/g++-16 /home/linuxbrew/.linuxbrew/bin/g++-15; do
    cxxpath="$(command -v "$cand" 2>/dev/null || true)"
    [[ -n "$cxxpath" ]] || { [[ -x "$cand" ]] && cxxpath="$cand" || continue; }
    if cxx_ok "$cxxpath"; then
      export CXX="$cxxpath"
      # derive the matching C compiler (g++-N → gcc-N, in the same dir)
      ccguess="$(dirname "$cxxpath")/$(basename "$cxxpath" | sed 's/g++/gcc/')"
      [[ -x "$ccguess" ]] && export CC="$ccguess"
      log "- C++20 compiler: $CXX${CC:+ (CC=$CC)}"
      break
    fi
  done
  if [[ -z "${CXX:-}" ]] || ! cxx_ok "${CXX:-c++}"; then
    log "! no C++20 compiler found — native deps (better-sqlite3) will fall back to"
    log "  a prebuilt if the node major ships one; otherwise the build will fail."
  fi
fi

# ── 3. npm install babysitter ──────────────────────────────────────────────
if [[ $FORCE -eq 1 || ! -d "$REPO/deps/babysitter/node_modules" ]]; then
  log "- npm install (babysitter)"
  ( cd "$REPO/deps/babysitter" && npm install --silent )
fi

# ── 4. build the babysitter SDK (no prepare hook → must be explicit) ────────
SDK_MAIN="$REPO/deps/babysitter/packages/sdk/dist/cli/main.js"
if [[ $FORCE -eq 1 || ! -f "$SDK_MAIN" ]]; then
  log "- build babysitter SDK (packages/sdk)"
  ( cd "$REPO/deps/babysitter" && npm --workspace packages/sdk run build )
fi
[[ -f "$SDK_MAIN" ]] || { echo "ERROR: babysitter SDK build did not produce $SDK_MAIN" >&2; exit 1; }

# ── 5. SDK resolver symlink for pinard-hosted processes ────────────────────
# processes/*.js do `import '@a5c-ai/babysitter-sdk'`; node walks up from the
# process file for node_modules/@a5c-ai/babysitter-sdk. bin/pinard only drops
# this symlink for PROJECT-hosted processes (outside $PINARD_REPO); for
# pinard-hosted ones it assumes the install created it — so create it here.
SDK_LINK="$REPO/node_modules/@a5c-ai/babysitter-sdk"
if [[ $FORCE -eq 1 || ! -e "$SDK_LINK" ]]; then
  log "- link @a5c-ai/babysitter-sdk"
  mkdir -p "$REPO/node_modules/@a5c-ai"
  ln -sfn "$REPO/deps/babysitter/packages/sdk" "$SDK_LINK"
fi

# ── 6. pi extension deps ───────────────────────────────────────────────────
for d in . pinard worker; do
  if [[ -f "$REPO/pi-extension/$d/package.json" && ( $FORCE -eq 1 || ! -d "$REPO/pi-extension/$d/node_modules" ) ]]; then
    log "- npm install (pi-extension/$d)"
    ( cd "$REPO/pi-extension/$d" && npm install --silent )
  fi
done

log "bootstrap complete"
