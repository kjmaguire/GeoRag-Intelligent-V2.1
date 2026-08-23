#!/usr/bin/env bash
# Tests for scripts/merge_clover.py.
#
# The merger exists because reporting either PHPUnit run's coverage alone
# is wrong in a flattering direction: the sqlite run skips every
# RequiresPostgres test (RLS, tenancy, PostGIS), and the pgsql config's
# testsuite list is narrower than the tree. So the cases that matter are
# the ones where the two runs DISAGREE -- a merger that quietly took the
# last file it read, or the first, would still print a plausible
# percentage.
#
# Run: bash scripts/tests/merge_clover_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MERGER="${REPO_ROOT}/scripts/merge_clover.py"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

clover() {
    # clover <path> <body>
    printf '<?xml version="1.0"?>\n<coverage><project>%s</project></coverage>\n' "$2" > "$1"
}

expect_pct() {
    # expect_pct <label> <expected> <args...>
    local label="$1" want="$2"; shift 2
    local got
    got="$("$PYTHON" "$MERGER" "${WORK}/out.xml" "$@" 2>/dev/null \
           | grep 'line coverage' | grep -oE '[0-9]+\.[0-9]%' || true)"
    if [ "$got" = "$want" ]; then
        printf 'ok   %s (%s)\n' "$label" "$got"; PASS=$((PASS + 1))
    else
        printf 'FAIL %s: expected %s, got %s\n' "$label" "$want" "${got:-<none>}"; FAIL=$((FAIL + 1))
    fi
}

# --- the case the merger exists for ----------------------------------
# One line is uncovered in the sqlite run and covered in the pgsql run.
# That is every RequiresPostgres test in the suite, in miniature. Taking
# either file alone reports 50%; the union is the truth.
clover "${WORK}/sqlite.xml" '<file name="/app/Rls.php"><line num="1" type="stmt" count="4"/><line num="2" type="stmt" count="0"/></file>'
clover "${WORK}/pgsql.xml"  '<file name="/app/Rls.php"><line num="1" type="stmt" count="0"/><line num="2" type="stmt" count="9"/></file>'
expect_pct "a line covered by only one run counts as covered" "100.0%" "${WORK}/sqlite.xml" "${WORK}/pgsql.xml"
expect_pct "order does not change the answer"                 "100.0%" "${WORK}/pgsql.xml" "${WORK}/sqlite.xml"

# --- a file only one run saw at all ----------------------------------
# Absent from a report is not the same as zero: the union of files, not
# the intersection, or every tenancy-only class would vanish from the
# denominator and inflate the percentage.
clover "${WORK}/only-a.xml" '<file name="/app/A.php"><line num="1" type="stmt" count="1"/></file>'
clover "${WORK}/only-b.xml" '<file name="/app/B.php"><line num="1" type="stmt" count="0"/></file>'
expect_pct "a file seen by one run still enters the denominator" "50.0%" "${WORK}/only-a.xml" "${WORK}/only-b.xml"

# --- nothing covered --------------------------------------------------
clover "${WORK}/empty.xml" '<file name="/app/C.php"><line num="1" type="stmt" count="0"/><line num="2" type="stmt" count="0"/></file>'
expect_pct "zero coverage reports zero, not a crash" "0.0%" "${WORK}/empty.xml"

# --- a missing report is reported AND fails the run ------------------
# Warning is not enough. Both PHPUnit steps in coverage.yml are
# continue-on-error, so this script's exit code is the only thing that
# can turn the job red when a run dies -- and a headline percentage
# merged from one report of two understates in exactly the flattering
# direction the merger exists to prevent, while reading as a real
# coverage drop to whoever looks next.
#
# Captured, not piped: `set -o pipefail` above means a pipeline into grep
# returns the merger's exit code, so the two claims have to be checked
# separately or they conflate.
PARTIAL_ERR="$("$PYTHON" "$MERGER" "${WORK}/out.xml" "${WORK}/only-a.xml" "${WORK}/nope.xml" 2>&1 >/dev/null)"
partial_rc=$?
if printf '%s' "$PARTIAL_ERR" | grep -q 'does not exist'; then
    printf 'ok   a missing report warns\n'; PASS=$((PASS + 1))
else
    printf 'FAIL a missing report was dropped silently\n'; FAIL=$((FAIL + 1))
fi
if [ "$partial_rc" -ne 0 ]; then
    printf 'ok   a partial merge is a non-zero exit\n'; PASS=$((PASS + 1))
else
    printf 'FAIL a partial merge published a headline figure and exited 0\n'; FAIL=$((FAIL + 1))
fi

# --- a truncated report is named, not a traceback --------------------
# A run killed by a timeout or an OOM leaves a half-written file, not a
# missing one. An unhandled ParseError in a step that is NOT
# continue-on-error fails the job with a stack trace instead of the one
# line that says which run died.
printf '<?xml version="1.0"?>\n<coverage><project><file name="/app/A.php"><line num="1" type="stmt" count="1"/></file' \
    > "${WORK}/truncated.xml"
TRUNC_ERR="$("$PYTHON" "$MERGER" "${WORK}/out.xml" "${WORK}/only-a.xml" "${WORK}/truncated.xml" 2>&1 >/dev/null)"
trunc_rc=$?
if printf '%s' "$TRUNC_ERR" | grep -q 'not parseable Clover XML'; then
    printf 'ok   a truncated report names itself\n'; PASS=$((PASS + 1))
else
    printf 'FAIL a truncated report was not identified\n'; FAIL=$((FAIL + 1))
fi
if [ "$trunc_rc" -ne 0 ] && ! printf '%s' "$TRUNC_ERR" | grep -q 'Traceback'; then
    printf 'ok   a truncated report fails the run without a traceback\n'; PASS=$((PASS + 1))
else
    printf 'FAIL a truncated report exited 0 or raised\n'; FAIL=$((FAIL + 1))
fi

# --- no readable reports at all is a failure, not 0%% -----------------
# A CI step that prints "0.0%" when PHPUnit never wrote a report at all
# reads as catastrophic coverage rather than as a broken job.
if "$PYTHON" "$MERGER" "${WORK}/out.xml" "${WORK}/nope.xml" >/dev/null 2>&1; then
    printf 'FAIL no readable reports exited 0\n'; FAIL=$((FAIL + 1))
else
    printf 'ok   no readable reports is a non-zero exit\n'; PASS=$((PASS + 1))
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
