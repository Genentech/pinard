#!/usr/bin/env bash
# Pinard pre-commit type-check gate.
#
# Runs `tsc --noEmit` over the Pi extensions whenever a commit touches a
# TypeScript file under pi-extension/. Pi loads these .ts files untranspiled,
# so this is the only thing that catches typos / undefined-variable bugs
# (e.g. `proj is not defined`) before they reach a running worker.
#
# Installed (composed alongside any existing git-annex hook) by ./install.
# Bypass for a single commit with: git commit --no-verify
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
EXT_DIR="$REPO_ROOT/pi-extension"

# Only run when staged changes touch extension TypeScript.
if ! git diff --cached --name-only | grep -q '^pi-extension/.*\.ts$'; then
  exit 0
fi

if [[ ! -d "$EXT_DIR/node_modules" ]]; then
  echo "pre-commit: pi-extension deps missing — run 'cd pi-extension && npm install' (skipping type-check)" >&2
  exit 0
fi

echo "pre-commit: type-checking Pi extensions..."
if ! (cd "$EXT_DIR" && npm run --silent typecheck); then
  echo "" >&2
  echo "pre-commit: TYPE-CHECK FAILED — commit blocked." >&2
  echo "Fix the errors above, or bypass with: git commit --no-verify" >&2
  exit 1
fi
echo "pre-commit: extensions type-check clean."
