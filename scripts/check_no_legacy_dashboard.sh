#!/usr/bin/env bash
# scripts/check_no_legacy_dashboard.sh — Foundry-cutover guard (2026-05-18)
#
# Fails the commit if any of the legacy pre-Foundry UI paths are
# reintroduced. The legacy tree was deleted on 2026-05-18 in favour of the
# Foundry redesign. See routes/web.php for the canonical Foundry routes.
#
# Forbidden patterns:
#   1. resources/js/Pages/Dashboard/   — the singular legacy page tree
#      (NOTE: Pages/Dashboards/ plural — the Customer Dashboards feature —
#      is fine; the script only flags exact "Dashboard/" not "Dashboards/")
#   2. resources/js/Components/Dashboard/ — components that only served the
#      legacy pages; deleted along with them
#   3. Inertia::render('Dashboard/...   — string-form renders into the
#      deleted page tree
#   4. @/Components/Dashboard/          — TS path-alias import into the
#      deleted component tree
#
# Run manually:
#   bash scripts/check_no_legacy_dashboard.sh
#
# Bypass (don't): pre-commit allows --no-verify, but reintroducing these
# paths means stale UI is back. Open a Wave-N follow-up ticket instead and
# revisit the cutover plan.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Build a regex of forbidden patterns. Each match prints the file:line:hit.
# Excluded paths: node_modules, vendor, .git, public/build (compiled assets
# may still reference old hashed chunk names harmlessly), and this script
# itself (it documents the patterns it bans).
EXCLUDES=(
  --exclude-dir=node_modules
  --exclude-dir=vendor
  --exclude-dir=.git
  --exclude-dir=build
  --exclude-dir=dist
  --exclude=check_no_legacy_dashboard.sh
  --exclude=.pre-commit-config.yaml
)

# Patterns require an UPPERCASE letter immediately after `Dashboard/`. This
# matches real TSX component / page references (Pages/Dashboard/Portfolio,
# @/Components/Dashboard/ProjectRoster, Inertia::render('Dashboard/Project'))
# while harmlessly ignoring prose mentions like "Pages/Dashboard/* deleted"
# in comments and docs. POSIX ERE has no negative lookahead, so the trailing
# `s` of `Dashboards` is filtered by the same uppercase-letter requirement
# (since `s` is lowercase).
PATTERN='resources/js/Pages/Dashboard/[A-Z]|resources/js/Components/Dashboard/[A-Z]|Inertia::render\(.Dashboard/[A-Z]|@/Components/Dashboard/[A-Z]'

HITS=$(grep -rEHn "$PATTERN" . "${EXCLUDES[@]}" 2>/dev/null || true)

if [ -n "$HITS" ]; then
  echo "ERROR: legacy pre-Foundry UI paths reintroduced." >&2
  echo "These were deleted on 2026-05-18 in the Foundry cutover." >&2
  echo "" >&2
  echo "$HITS" >&2
  echo "" >&2
  echo "If you are reverting the cutover (not just adding a Pages/Dashboards/" >&2
  echo "plural feature), discuss with Kyle first — this guard exists to make" >&2
  echo "the cutover deliberate, not silently re-add the deprecated UI." >&2
  exit 1
fi

exit 0
