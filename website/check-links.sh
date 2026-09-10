#!/usr/bin/env bash
# check-links.sh — guard against root-absolute asset/link paths that would 404
# when the site is served from a project-pages subpath (e.g. /pinard/).
#
# The site is baseURL-agnostic: every internal ref must be either relative
# (resolved via <base href>) or already under the deploy base path. A bare
# root-absolute path like /images/... or /css/... breaks on GitHub Pages.
#
# Usage:  check-links.sh <built-dir> <base-path>
#   e.g.  check-links.sh public /pinard/     (Pages subpath)
#         check-links.sh public /            (root deploy — passes trivially)
set -euo pipefail

DIR="${1:-public}"
BASE="${2:-/}"

# Match root-absolute src/href/url() (start with "/<letter>"), then drop the ones
# already under BASE. External (https://), anchors (#), protocol-relative (//),
# data:, and relative (no leading /) refs are never matched.
hits="$(grep -rEn \
          '(src|href)="/[a-zA-Z][^"]*"|url\((["'"'"']?)/[a-zA-Z][^)]*\)' \
          "$DIR" 2>/dev/null \
        | grep -vE "(src|href)=\"${BASE}|url\((\"|')?${BASE}" \
        || true)"

if [ -n "$hits" ]; then
  echo "check-links: FAIL — root-absolute paths that will 404 under ${BASE}:" >&2
  echo "$hits" | head -30 >&2
  echo "" >&2
  echo "Fix: use a relative path (resolved via <base href>) instead of a leading '/'." >&2
  exit 1
fi
echo "check-links: OK — no stray root-absolute paths (base=${BASE}, dir=${DIR})"
