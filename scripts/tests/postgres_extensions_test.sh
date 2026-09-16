#!/usr/bin/env bash
# Discrimination tests for scripts/check-postgres-extensions.py.
#
# Compose and RDS get their extensions from two different mechanisms — an
# automatic docker-entrypoint-initdb.d run vs a bootstrap.sql the operator
# executes once by hand — so nothing but this check keeps them in step.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="$ROOT/scripts/check-postgres-extensions.py"
PASS=0; FAIL=0
ok()  { printf 'ok   %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL+1)); }

# fixture <compose-sql> <bootstrap-sql> [extra-file-path] [extra-file-body]
fixture() {
  local dir; dir="$(mktemp -d)"
  mkdir -p "$dir/docker/postgresql/init" "$dir/deploy/aws" "$dir/database"
  printf '%s\n' "$1" > "$dir/docker/postgresql/init/10-init.sql"
  printf '%s\n' "$2" > "$dir/deploy/aws/bootstrap.sql"
  if [ -n "${3:-}" ]; then mkdir -p "$dir/$(dirname "$3")"; printf '%s\n' "${4:-}" > "$dir/$3"; fi
  echo "$dir"
}

run() {
  local want="$1" label="$2" dir="$3" rc
  python3 "$CHECK" "$dir" >/dev/null 2>&1; rc=$?
  rm -rf "$dir"
  [ "$rc" -eq "$want" ] && ok "$label" || bad "$label (exit $rc, want $want)"
}

BASE_C='CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;'
BASE_A='CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;'

run 0 "matching lists pass" "$(fixture "$BASE_C" "$BASE_A")"

run 1 "an extension added to compose but NOT bootstrap.sql fails" \
  "$(fixture "$BASE_C
CREATE EXTENSION IF NOT EXISTS pgrouting;" "$BASE_A")"

run 1 "an extension in bootstrap.sql but NOT compose fails" \
  "$(fixture "$BASE_C" "$BASE_A
CREATE EXTENSION IF NOT EXISTS pgrouting;")"

run 0 "the three documented omissions stay quiet" \
  "$(fixture "$BASE_C
CREATE EXTENSION IF NOT EXISTS auto_explain;
CREATE EXTENSION IF NOT EXISTS pg_ivm;
CREATE EXTENSION IF NOT EXISTS pg_stat_kcache;" "$BASE_A")"

run 1 "...but a pg_ivm CALL SITE expires its justification" \
  "$(fixture "$BASE_C
CREATE EXTENSION IF NOT EXISTS pg_ivm;" "$BASE_A" \
    "database/raw/matview.sql" "CREATE INCREMENTAL MATERIALIZED VIEW m AS SELECT 1;")"

run 1 "...and a pg_stat_kcache CALL SITE does too" \
  "$(fixture "$BASE_C
CREATE EXTENSION IF NOT EXISTS pg_stat_kcache;" "$BASE_A" \
    "database/raw/stats.sql" "SELECT * FROM pg_stat_kcache;")"

run 0 "a COMMENT mentioning pg_stat_kcache is not a call site" \
  "$(fixture "$BASE_C
CREATE EXTENSION IF NOT EXISTS pg_stat_kcache;" "$BASE_A" \
    "database/raw/notes.sql" "-- pg_stat_kcache was dropped for RDS")"

run 1 "a documented omission that bootstrap.sql DOES create fails" \
  "$(fixture "$BASE_C
CREATE EXTENSION IF NOT EXISTS pg_ivm;" "$BASE_A
CREATE EXTENSION IF NOT EXISTS pg_ivm;")"

python3 "$CHECK" >/dev/null 2>&1 \
  && ok "the real tree passes" \
  || bad "the real tree passes"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
