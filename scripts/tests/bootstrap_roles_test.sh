#!/usr/bin/env bash
# Discrimination tests for scripts/check-bootstrap-roles.py.
#
# The checker passes against the tree it was written on, which proves
# nothing. Each case below rebuilds bootstrap.sql in one of the shapes that
# actually shipped on 2026-09-16 and asserts the checker rejects it, naming
# the live failure that shape produces.
#
# Run: bash scripts/tests/bootstrap_roles_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check-bootstrap-roles.py"
PYTHON="${PYTHON:-python3}"

PASS=0
FAIL=0
ok()    { printf '\033[32m  ✓ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '\033[31m  ✗ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
case_() { printf '\033[34m%s\033[0m\n' "$*"; }

# A fixture the checker passes cleanly, so each case introduces exactly one
# defect and the failure is attributable to it.
make_fixture() {
  local d
  d="$(mktemp -d)"
  mkdir -p "$d/scripts" "$d/deploy/aws" "$d/database/migrations"
  cp "$CHECKER" "$d/scripts/check-bootstrap-roles.py"

  cat >"$d/deploy/aws/bootstrap.sql" <<'SQL'
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE georag_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS', :'georag_app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_app')\gexec
ALTER ROLE georag_app LOGIN PASSWORD :'georag_app_password';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_read') THEN
    CREATE ROLE georag_read NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_write') THEN
    CREATE ROLE georag_write NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_audit') THEN
    CREATE ROLE georag_audit NOLOGIN;
  END IF;
END $$;

SELECT format('CREATE ROLE martin_readonly LOGIN PASSWORD %L', :'martin_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'martin_readonly')\gexec
ALTER ROLE martin_readonly LOGIN PASSWORD :'martin_password';

SELECT format('CREATE ROLE hatchet LOGIN PASSWORD %L', :'hatchet_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hatchet')\gexec
ALTER ROLE hatchet LOGIN PASSWORD :'hatchet_password';
SQL

  cat >"$d/database/migrations/2026_04_09_173750_provision_core_schemas_and_roles_for_test_db.php" <<'PHP'
<?php
// DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_app')
// THEN CREATE ROLE georag_app NOLOGIN; END IF; END $$;
PHP
  echo "$d"
}

run_checker() { ( cd "$1" && "$PYTHON" scripts/check-bootstrap-roles.py 2>&1 ); }

# ---------------------------------------------------------------------------
case_ "baseline — a fixture with every role correct"
D=$(make_fixture)
if OUT=$(run_checker "$D"); then
  ok "passes a correct bootstrap.sql"
else
  bad "clean fixture rejected: $(head -3 <<<"$OUT" | tr '\n' ' ')"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the real repository"
if OUT=$(cd "$REPO_ROOT" && "$PYTHON" scripts/check-bootstrap-roles.py 2>&1); then
  ok "passes against the committed tree"
else
  bad "committed tree rejected: $(head -5 <<<"$OUT" | tr '\n' ' ')"
fi

# ---------------------------------------------------------------------------
case_ "martin_readonly NOLOGIN — the shape that shipped"
# Live failure: the martin task gets FATAL: role "martin_readonly" is not
# permitted to log in, fails its health check, and every MVT layer serves
# nothing. Compose hid it by connecting martin as georag_app instead.
D=$(make_fixture)
$PYTHON - "$D" <<'PY'
import pathlib, sys, re
p = pathlib.Path(sys.argv[1]) / "deploy/aws/bootstrap.sql"
s = p.read_text()
s = re.sub(r"SELECT format\('CREATE ROLE martin_readonly[^\n]*\n[^\n]*\\gexec\nALTER ROLE martin_readonly[^\n]*\n",
           "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='martin_readonly') THEN CREATE ROLE martin_readonly NOLOGIN NOINHERIT; END IF; END $$;\n", s)
p.write_text(s)
PY
OUT=$(run_checker "$D")
if grep -q 'martin_readonly' <<<"$OUT" && grep -q 'not permitted to log in' <<<"$OUT"; then
  ok "rejects a NOLOGIN martin_readonly and names the FATAL it produces"
else
  bad "accepted a NOLOGIN martin_readonly"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "georag_app never granted LOGIN — every container down"
D=$(make_fixture)
$PYTHON - "$D" <<'PY'
import pathlib, sys, re
p = pathlib.Path(sys.argv[1]) / "deploy/aws/bootstrap.sql"
s = p.read_text()
s = re.sub(r"SELECT format\('CREATE ROLE georag_app[^\n]*\n[^\n]*\\gexec\nALTER ROLE georag_app[^\n]*\n", "", s)
p.write_text(s)
PY
OUT=$(run_checker "$D")
if grep -q 'georag_app' <<<"$OUT" && grep -q 'never grants it LOGIN' <<<"$OUT"; then
  ok "rejects a bootstrap.sql that omits georag_app entirely"
else
  bad "accepted a tree where georag_app never gets LOGIN"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "LOGIN only on creation — repairs no existing database"
# The subtle half. Every database that exists today already has these roles,
# NOLOGIN, so a create-time-only fix fires for none of them -- and re-running
# bootstrap.sql is documented as the way to verify it.
D=$(make_fixture)
$PYTHON - "$D" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]) / "deploy/aws/bootstrap.sql"
s = p.read_text().replace("ALTER ROLE martin_readonly LOGIN PASSWORD :'martin_password';\n", "")
p.write_text(s)
PY
OUT=$(run_checker "$D")
if grep -q 'only where the role is created' <<<"$OUT"; then
  ok "rejects LOGIN granted only inside the create-if-absent"
else
  bad "accepted a create-time-only LOGIN grant"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "psql :'var' inside a dollar-quoted block — the shape that shipped"
# Live failure, verified against PostgreSQL 16: psql substitutes in its own
# lexer and treats $$...$$ as one opaque token, so the server receives a
# literal ':' and answers `syntax error at or near ":"`. Under
# ON_ERROR_STOP that aborts the first command of the go-live sequence.
D=$(make_fixture)
cat >>"$D/deploy/aws/bootstrap.sql" <<'SQL'

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'other') THEN
    CREATE ROLE other LOGIN PASSWORD :'other_password';
  END IF;
END $$;
SQL
OUT=$(run_checker "$D")
if grep -q "dollar-quoted block" <<<"$OUT" && grep -q "syntax error" <<<"$OUT"; then
  ok "rejects :'var' inside \$\$...\$\$ and names the server error"
else
  bad "accepted a :'var' inside a dollar-quoted block"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "a grant-holder role gains LOGIN — the other direction"
D=$(make_fixture)
cat >>"$D/deploy/aws/bootstrap.sql" <<'SQL'

ALTER ROLE georag_read LOGIN PASSWORD 'x';
SQL
OUT=$(run_checker "$D")
if grep -q 'georag_read' <<<"$OUT" && grep -q 'widens the credential surface' <<<"$OUT"; then
  ok "rejects LOGIN on a role nothing connects as"
else
  bad "accepted LOGIN on a grant-holder role"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the operator is pointed at a secret key that does not exist"
# The shape that shipped: bootstrap.sql said `jq -r .HATCHET_DB_PASSWORD`,
# which is a real key name -- in compose. georag/app has never had it. `jq -r`
# prints the string "null" for a missing key, so the role's password becomes
# the literal four characters `null`, from a command that looked like it
# worked, and authentication fails later from somewhere else entirely.
D=$(make_fixture)
mkdir -p "$D/deploy/aws"
cat >"$D/deploy/aws/README.md" <<'MD'
| Key | Read by | What it is |
| --- | --- | --- |
| `GEORAG_APP_PASSWORD` | every application service | the app role's password |
| `HATCHET_DATABASE_URL` | hatchet | full connection string |
MD
cat >>"$D/deploy/aws/bootstrap.sql" <<'SQL'
-- \set hatchet_password `... | jq -r .HATCHET_DB_PASSWORD`
SQL
OUT=$(run_checker "$D")
if grep -q 'HATCHET_DB_PASSWORD' <<<"$OUT" && grep -q 'not in the secret table' <<<"$OUT"; then
  ok "rejects a jq reference to a key georag/app does not have"
else
  bad "accepted a reference to a non-existent secret key"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "the migration drops its IF NOT EXISTS guard"
# bootstrap.sql wins by getting there first. Without the guard the migration
# hits CREATE ROLE on a role that exists and fails the whole migrate task.
D=$(make_fixture)
cat >"$D/database/migrations/2026_04_09_173750_provision_core_schemas_and_roles_for_test_db.php" <<'PHP'
<?php
// CREATE ROLE georag_app NOLOGIN;
PHP
OUT=$(run_checker "$D")
if grep -q 'no longer guarded by IF NOT EXISTS' <<<"$OUT"; then
  ok "rejects an unguarded CREATE ROLE in the migration"
else
  bad "accepted an unguarded CREATE ROLE georag_app"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
echo
if [ "$FAIL" = "0" ]; then
  printf '\033[32m%d passed, 0 failed\033[0m\n' "$PASS"; exit 0
else
  printf '\033[31m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"; exit 1
fi
