#!/usr/bin/env bash
# Discrimination tests for scripts/check-no-committed-secrets.php.
#
# This checker reported "no committed credentials found in 3144 tracked
# file(s)" on 2026-09-15 while two tracked files carried a live Redis
# password that had been publicly readable on GitHub. Both misses were
# shape, not entropy — the value was 31 characters of mixed case and digits,
# exactly what looksGenerated() is built to catch:
#
#   scripts/phase21_step1_verify.sh   REDIS_PWD='<value>'
#       The name pattern matched PASSWORD|PASSWD. `PWD` abbreviates past it.
#
#   scripts/phase0_wrapper_smoke.sh   redis-cli -a '<value>'
#       Every pattern matched an ASSIGNMENT. A credential passed as a
#       command-line argument is not one, so nothing examined the line.
#
# A green run of this checker is a claim about 3000+ files, so the cases
# below assert it can still say no. Each introduces exactly one shape; the
# false-alarm cases matter as much, because a checker that cries wolf on
# `--requirepass "$REDIS_PASSWORD"` gets deleted and then catches nothing.
#
# Run: bash scripts/tests/committed_secrets_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check-no-committed-secrets.php"

# Credential-shaped, and deliberately NOT any value this repository has ever
# used: a test fixture that carries a real burned secret re-commits it.
FAKE='Qv7RmKdXp2LbTnHs4WyZcFj9AeUgB3x'
# Hits are reported as a fingerprint, not the value (SEC-6): CI logs are not
# a place to keep a second copy of a leaked credential.
fp() { printf '%s' "$1" | sha256sum | cut -c1-12; }
FAKE_FP="$(fp "$FAKE")"
# secrets.token_urlsafe(48)-shaped: separators are the norm, not a
# placeholder signal. Also never a real value.
FAKE_URLSAFE='kP3v_Qm8-ZrT2xLwN9sB-hY4cJ7fD1gA6eUo5_iRq0MtVnW2yXzKb8Hj3LdPs'
FAKE_URLSAFE_FP="$(fp "$FAKE_URLSAFE")"
# 32 random-looking bytes, base64 — the shape of a Laravel APP_KEY.
# Split across two quotes so the repo-wide run does not flag its own fixture.
FAKE_APP_KEY="base64:"'k3Jx9QmZ2vT7rB8nL1wP5sD0yF6hG4aE2cU9iO7tR1M='
FAKE_APP_KEY_FP="$(fp "$FAKE_APP_KEY")"

PASS=0
FAIL=0
ok()    { printf '\033[32m  ✓ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '\033[31m  ✗ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
case_() { printf '\033[34m%s\033[0m\n' "$*"; }

# The checker enumerates via `git ls-files`, so a fixture needs to be a real
# repository with the file actually added.
make_fixture() {
  local d
  d="$(mktemp -d)"
  mkdir -p "$d/scripts"
  cp "$CHECKER" "$d/scripts/check-no-committed-secrets.php"
  printf '# placeholder\nREDIS_PASSWORD=georag_dev_password\n' >"$d/.env.example"
  git -C "$d" init -q
  git -C "$d" add -A
  echo "$d"
}

# add_line <dir> <relpath> <content>
add_line() {
  printf '%s\n' "$3" >"$1/$2"
  git -C "$1" add "$2"
}

run_checker() { ( cd "$1" && php scripts/check-no-committed-secrets.php 2>&1 ); }

# ---------------------------------------------------------------------------
case_ "baseline — a tree whose only credential-shaped value is a placeholder"
D=$(make_fixture)
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "passes clean"; else bad "should pass; got: $OUT"; fi
if grep -q "tracked file(s)" <<<"$OUT"; then ok "says how many files it looked at"; else bad "should report its scope"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the abbreviation hole — REDIS_PWD= is a password assignment"
D=$(make_fixture)
add_line "$D" scripts/verify.sh "REDIS_PWD='${FAKE}'"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "$FAKE_FP" <<<"$OUT"; then
  ok "caught, and prints a fingerprint that identifies which one to rotate"
else
  bad "PWD assignment slipped through; rc=$RC: $OUT"
fi
if grep -q "$FAKE" <<<"$OUT"; then bad "printed the credential itself"; else ok "does not print the credential"; fi
if grep -q "scripts/verify.sh:1" <<<"$OUT"; then ok "names file and line"; else bad "should name file:line"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the argument hole — a credential passed as a flag, not assigned"
D=$(make_fixture)
add_line "$D" scripts/smoke.sh "docker exec georag-redis redis-cli -a '${FAKE}' --no-auth-warning PING"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "$FAKE_FP" <<<"$OUT"; then
  ok "caught redis-cli -a"
else
  bad "the exact shape that survived ee853f5 still passes; rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the server half — --requirepass with a literal"
D=$(make_fixture)
add_line "$D" scripts/run.sh "redis-server --requirepass '${FAKE}' --appendonly yes"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "$FAKE_FP" <<<"$OUT"; then
  ok "caught --requirepass"
else
  bad "a hardcoded requirepass passes; rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the shell-default near-miss — \${REDIS_PWD:-literal}"
# The `${VAR:-default}` form needed its own pattern because `[:=>]+` eats the
# `:` of `:-` and the capture then starts at a dash, which reads as a
# placeholder. That fix was made for PASSWORD names; PWD needs it too.
D=$(make_fixture)
add_line "$D" scripts/default.sh "pw=\"\${REDIS_PWD:-${FAKE}}\""
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "$FAKE_FP" <<<"$OUT"; then
  ok "caught the shell default"
else
  bad "the :- near-miss is back for PWD names; rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "no false alarm — interpolated forms are how correct code looks"
# These three lines are live in docker-compose.yml, deploy/aws/terraform and
# the phase scripts. Flagging any of them makes the check unusable.
D=$(make_fixture)
add_line "$D" scripts/ok1.sh 'redis-server --requirepass "$REDIS_PASSWORD"'
add_line "$D" scripts/ok2.sh 'redis-cli -a "$REDIS_PWD" --no-auth-warning PING'
add_line "$D" scripts/ok3.sh 'command: redis-server --requirepass ${REDIS_PASSWORD}'
add_line "$D" scripts/ok4.sh "redis_pw=\"\$(redis_password)\"; redis-cli -a \"\$redis_pw\" PING"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "reads the credential from the environment without complaint"; else bad "false alarm on correct code: $OUT"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "no false alarm — placeholders stay allowed by name"
D=$(make_fixture)
add_line "$D" scripts/ph.sh "REDIS_PWD=georag_dev_password"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "an ALLOWED placeholder under a PWD name still passes"; else bad "placeholder rejected: $OUT"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "regression — the shapes that already worked still work"
D=$(make_fixture)
add_line "$D" scripts/pg.sh "POSTGRES_PASSWORD=${FAKE}"
add_line "$D" scripts/dsn.sh "DATABASE_URL=postgres://georag:${FAKE}@postgresql:5432/georag"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "scripts/pg.sh" <<<"$OUT" && grep -q "scripts/dsn.sh" <<<"$OUT"; then
  ok "assignment and DSN detection survived the widening"
else
  bad "widening the patterns broke an existing one; rc=$RC: $OUT"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "SEC-6 — *_KEY names are inspected, and separators are not a free pass"
# A real APP_KEY and FASTAPI_SERVICE_KEY sat in ops/audit/ while this reported
# clean: KEY was not a name it looked at, and token_urlsafe output (always
# carrying - and _) was read as a placeholder.
D=$(make_fixture)
add_line "$D" scripts/dump.yml "      FASTAPI_SERVICE_KEY: ${FAKE_URLSAFE}"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "$FAKE_URLSAFE_FP" <<<"$OUT"; then
  ok "caught a token_urlsafe-shaped service key"
else
  bad "a *_KEY with - and _ still passes; rc=$RC: $OUT"
fi
rm -rf "$D"

D=$(make_fixture)
add_line "$D" scripts/dump.yml "      APP_KEY: ${FAKE_APP_KEY}"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ] && grep -q "$FAKE_APP_KEY_FP" <<<"$OUT"; then
  ok "caught a base64: APP_KEY"
else
  bad "a Laravel APP_KEY passes; rc=$RC: $OUT"
fi
rm -rf "$D"

D=$(make_fixture)
add_line "$D" config.xml "<env name=\"SOME_SETTING\" value=\"${FAKE_APP_KEY}\"/>"
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -ne 0 ]; then ok "a base64: key is caught whatever it is assigned to"; else bad "base64 key under an innocuous name passes"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "SEC-6 — no false alarm on the placeholders and code the wider net now reaches"
D=$(make_fixture)
# phpunit.pgsql.xml's key: decodes to "phpunit-testing-key-not-a-secret".
add_line "$D" a.xml '<env name="APP_KEY" value="base64:cGhwdW5pdC10ZXN0aW5nLWtleS1ub3QtYS1zZWNyZXQ="/>'
# phpunit.xml's key: decodes to 32 x "a".
add_line "$D" b.xml '<env name="APP_KEY" value="base64:YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="/>'
add_line "$D" c.sh '--set "secrets.fastapiServiceKey=CHANGEME-rotate-this-key-to-32plus-chars-from-prod-secret"'
add_line "$D" d.py 'FASTAPI_SERVICE_KEY = "test-service-key-must-be-at-least-32-bytes-long"'
add_line "$D" e.py 'max_tokens=settings.ANTHROPIC_MAX_OUTPUT_TOKENS,'
add_line "$D" f.py 'minio_key="collars/5ec10000-0000-4000-8000-00000000000a/20260824_204518_Assays.csv"'
add_line "$D" g.ts '    commodity_keys_3d: CommodityKey3D[];'
add_line "$D" h.yml '      WORKSPACE_KEY: 5ec10000-0000-4000-8000-00000000000a'
add_line "$D" i.tf 'reverb_app_key = "georag-reverb-app-key-REVERB_APP_KEY-for-local-only"'
add_line "$D" j.yml '      APP_KEY: ${APP_KEY:?APP_KEY must be set in .env}'
OUT=$(run_checker "$D"); RC=$?
if [ "$RC" -eq 0 ]; then ok "placeholders, identifiers, object keys and UUIDs pass"; else bad "false alarm: $OUT"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the checker still tells you the load-bearing step"
D=$(make_fixture)
add_line "$D" scripts/verify.sh "REDIS_PWD='${FAKE}'"
OUT=$(run_checker "$D")
if grep -q "Rotate it" <<<"$OUT"; then
  ok "says rotate first — removing it from the tree does not remove it from history"
else
  bad "the remediation text is gone"
fi
rm -rf "$D"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
