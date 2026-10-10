#!/usr/bin/env bash
# Discrimination tests for scripts/check-ci-path-filters.py.
#
# The checker guards a silent hole: a `paths-ignore` entry (or a `paths`
# filter that never puts a file back) that skips a document CI parses as DATA
# lets a real breakage through with a green PR. A checker that passes on the
# tree it was written against has demonstrated nothing, so each case below
# introduces exactly the shape it exists to reject.
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
      - 'docs/api/**'
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
if [ "$RC" -eq 0 ]; then ok "accepts docs/api/** and root *.md"; else bad "should pass; got: $OUT"; fi
if grep -q "2 workflow(s) filtered" <<<"$OUT"; then ok "reports how many it actually checked"; else bad "should say what it checked"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the trap — an ignore glob that swallows deploy/aws/README.md"
# Live consequence: check-ecs-secret-keys.py parses that README's key table and
# aws-preflight.sh A-10 derives the go-live key list from it. Ignoring it means
# a broken key table merges green.
D=$(make_fixture)
sed -i "s|- 'docs/api/\*\*'|- '**/*.md'|" "$D/.github/workflows/ci.yml"
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
sed -i "s|- 'docs/api/\*\*'|- '*.html'|" "$D/.github/workflows/docker-build.yml"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "georag-architecture.html" <<<"$OUT"; then
  ok "rejects '*.html', and names the parity test"
else
  bad "should have rejected '*.html'; got rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the docs/** trap — two docs under it are DATA to blocking tests"
# Live consequence: test_cron_doc_parity.py parses the manual's cron tables and
# test_lookup_and_rescope.py reads ADR-0014; both run in the blocking pytest
# job. `docs/**` in paths-ignore skipped CI for a push that broke either, so it
# merged green. This is the filter ci.yml and docker-build.yml used to carry.
D=$(make_fixture)
sed -i "s|- 'docs/api/\*\*'|- 'docs/**'|" "$D/.github/workflows/ci.yml"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "07-orchestration.md" <<<"$OUT" && grep -q "0014-workspace-lookup-and-pivot.md" <<<"$OUT"; then
  ok "rejects 'docs/**', and names both documents"
else
  bad "should have rejected 'docs/**'; got rc=$RC: $OUT"
fi
if grep -q "test_cron_doc_parity" <<<"$OUT" && grep -q "test_lookup_and_rescope" <<<"$OUT"; then
  ok "names the tests that read them"
else
  bad "should name the readers; got: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
# docs/** cannot be carved out of paths-ignore (it has no negation), so the
# shipped workflows use `paths`: '!' removes a file and a LATER plain pattern
# puts it back. The fixture below is that shape, and the cases after it break
# it one way at a time.
make_paths_fixture() {
  local d wf
  d="$(make_fixture)"
  for wf in ci docker-build; do
    cat >"$d/.github/workflows/${wf}.yml" <<'YML'
on:
  pull_request:
    branches: [main]
    paths:
      - '**'
      - '!docs/**'
      - '!*.md'
      - 'docs/architecture/manual/07-orchestration.md'
      - 'docs/adr/0014-workspace-lookup-and-pivot.md'
jobs:
  noop:
    runs-on: ubuntu-latest
YML
  done
  echo "$d"
}

case_ "a paths filter with the two documents put back is accepted"
D=$(make_paths_fixture)
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts '!docs/**' followed by the two re-includes"; else bad "should pass; got: $OUT"; fi
if grep -q "2 workflow(s) filtered" <<<"$OUT"; then ok "counts a paths filter as checked, not as nothing"; else bad "should say what it checked: $OUT"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "a paths filter that forgets to put a document back"
D=$(make_paths_fixture)
sed -i "/0014-workspace-lookup-and-pivot.md'/d" "$D/.github/workflows/ci.yml"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "0014-workspace-lookup-and-pivot.md" <<<"$OUT" && grep -q "'!docs/\*\*'" <<<"$OUT"; then
  ok "rejects the missing re-include, and names the pattern that removed it"
else
  bad "should have rejected the missing re-include; got rc=$RC: $OUT"
fi
if ! grep -q "07-orchestration.md" <<<"$OUT"; then ok "does not blame the document that WAS put back"; else bad "false alarm on 07-orchestration.md: $OUT"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "order matters — a re-include ABOVE the '!' is removed again"
# GitHub evaluates the patterns top to bottom and the last match wins. A
# re-include written above '!docs/**' reads as if it protected the file and
# protects nothing; a checker that merely looked for the string would pass it.
D=$(make_paths_fixture)
"$PYTHON" - "$D/.github/workflows/docker-build.yml" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
keep = [l for l in p.read_text().splitlines()
        if "07-orchestration" not in l and "0014-workspace" not in l]
at = next(i for i, l in enumerate(keep) if l.strip() == "- '**'") + 1
keep[at:at] = ["      - 'docs/architecture/manual/07-orchestration.md'",
               "      - 'docs/adr/0014-workspace-lookup-and-pivot.md'"]
p.write_text("\n".join(keep) + "\n")
PY
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "docker-build.yml" <<<"$OUT" && grep -q "07-orchestration.md" <<<"$OUT"; then
  ok "rejects a re-include that a later '!docs/**' undoes"
else
  bad "should have rejected the mis-ordered re-include; got rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "a paths filter that never includes a document at all"
D=$(make_paths_fixture)
cat >"$D/.github/workflows/ci.yml" <<'YML'
on:
  pull_request:
    paths:
      - 'src/**'
jobs:
  noop:
    runs-on: ubuntu-latest
YML
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "no pattern includes it" <<<"$OUT" && grep -q "deploy/aws/README.md" <<<"$OUT"; then
  ok "rejects paths: ['src/**'] — a doc outside the include list is skipped"
else
  bad "should have rejected paths: ['src/**']; got rc=$RC: $OUT"
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
