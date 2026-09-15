#!/usr/bin/env bash
# Discrimination tests for scripts/check-ci-path-filters.py.
#
# The checker guards a silent hole: a `paths-ignore` entry that skips a
# document CI parses as DATA lets a real breakage through with a green PR.
# A checker that passes on the tree it was written against has demonstrated
# nothing, so each case below introduces exactly the shape it exists to
# reject.
#
# Run: bash scripts/tests/ci_path_filter_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check-ci-path-filters.py"
PYTHON="$(command -v python3 || command -v python)"

PASS=0
FAIL=0
ok()    { printf '\033[32m  ✓ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '\033[31m  ✗ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
case_() { printf '\033[34m%s\033[0m\n' "$*"; }

# A fixture repo the checker passes cleanly, so each test adds one defect.
make_fixture() {
  local d
  d="$(mktemp -d)"
  mkdir -p "$d/.github/workflows" "$d/scripts" "$d/deploy/aws"
  cp "$CHECKER" "$d/scripts/check-ci-path-filters.py"
  touch "$d/deploy/aws/README.md" "$d/georag-architecture.html"
  for wf in ci docker-build; do
    cat >"$d/.github/workflows/${wf}.yml" <<'YML'
on:
  pull_request:
    branches: [main]
    paths-ignore:
      - 'docs/**'
      - '*.md'
jobs:
  noop:
    runs-on: ubuntu-latest
YML
  done
  echo "$d"
}

run_checker() { ( cd "$1" && "$PYTHON" scripts/check-ci-path-filters.py 2>&1 ); }

# ---------------------------------------------------------------------------
case_ "baseline — filters that skip only inert prose"
D=$(make_fixture)
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts docs/** and root *.md"; else bad "should pass; got: $OUT"; fi
if grep -q "2 workflow(s) filtered" <<<"$OUT"; then ok "reports how many it actually checked"; else bad "should say what it checked"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the trap — an ignore glob that swallows deploy/aws/README.md"
# Live consequence: check-ecs-secret-keys.py parses that README's key table and
# aws-preflight.sh A-10 derives the go-live key list from it. Ignoring it means
# a broken key table merges green.
D=$(make_fixture)
sed -i "s|- 'docs/\*\*'|- '**/*.md'|" "$D/.github/workflows/ci.yml"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "deploy/aws/README.md" <<<"$OUT"; then
  ok "rejects '**/*.md', and names the file and its reader"
else
  bad "should have rejected '**/*.md'; got rc=$RC: $OUT"
fi
if grep -q "check-ecs-secret-keys" <<<"$OUT"; then ok "names WHAT reads it, so the claim is checkable"; else bad "should name the reader"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the other trap — the architecture doc"
# tests/Unit/ArchitectureDocSchemaParityTest.php fails when the doc names a
# schema.table no migration creates. Ignore it and that gate stops existing.
D=$(make_fixture)
sed -i "s|- 'docs/\*\*'|- '*.html'|" "$D/.github/workflows/docker-build.yml"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "georag-architecture.html" <<<"$OUT"; then
  ok "rejects '*.html', and names the parity test"
else
  bad "should have rejected '*.html'; got rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "root-level globs must not match nested paths"
# The whole filter rests on GitHub's rule that a single `*` does not cross `/`.
# A checker using plain fnmatch would flag `*.md` against deploy/aws/README.md
# and cry wolf until someone deleted the check.
D=$(make_fixture)
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "'*.md' is not treated as matching deploy/aws/README.md"; else bad "false alarm on root-level glob: $OUT"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "a run that checked nothing is not a pass"
# The absence-as-success shape, in the checker itself: if the parser stops
# finding paths-ignore blocks it would report success having verified nothing.
D=$(make_fixture)
for wf in ci docker-build; do
  "$PYTHON" - "$D/.github/workflows/${wf}.yml" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
p.write_text("\n".join(l for l in p.read_text().splitlines()
                       if "paths-ignore" not in l and not l.strip().startswith("- '")) + "\n")
PY
done
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "verifies nothing\|no workflow carried" <<<"$OUT"; then
  ok "fails when no filter is found at all, rather than passing empty"
else
  bad "a checker that found nothing reported success; rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "a missing workflow is a failure, not a skip"
D=$(make_fixture)
rm "$D/.github/workflows/docker-build.yml"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "docker-build.yml does not exist" <<<"$OUT"; then
  ok "names a workflow that vanished"
else
  bad "should have failed on the missing workflow; rc=$RC: $OUT"
fi
rm -rf "$D"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
