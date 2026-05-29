#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step7_verify.sh
#
# Phase 2 Step 7 done-definition — Activepieces flow-run observability.
#
#   1. grafana_hatchet_readonly role exists, NOSUPERUSER + NOBYPASSRLS
#   2. Role can SELECT from v1_runs_olap (datasource creds work)
#   3. Role CANNOT INSERT into v1_runs_olap (read-only is real)
#   4. Hatchet datasource provisioning YAML present
#   5. Integrations dashboard JSON present + valid JSON
#   6. Dashboard JSON contains the expected uid + 4 panels + the
#      Activepieces-driven workflow names
#   7. Each dashboard SQL query runs against the hatchet DB without error
#
# Live-Grafana rendering depends on `docker compose --profile dev-monitor`
# being up — that's an operational concern.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
GRAFANA_HATCHET_PASSWORD=$(awk -F= '/^GRAFANA_HATCHET_READONLY_PASSWORD=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
REPO="${REPO_ROOT:-/home/georag/projects/georag}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 2 STEP 7 — OTel + GRAFANA VERIFICATION
============================================================
BANNER

# 1) Role posture
posture=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT (CASE WHEN rolsuper THEN 'super' ELSE 'nosuper' END) || '/' ||
           (CASE WHEN rolbypassrls THEN 'bypass' ELSE 'nobypass' END) || '/' ||
           (CASE WHEN rolcanlogin THEN 'login' ELSE 'nologin' END)
    FROM pg_roles WHERE rolname = 'grafana_hatchet_readonly';" 2>/dev/null | tr -d ' ')
[ "$posture" = "nosuper/nobypass/login" ] \
    && check "grafana_hatchet_readonly role: NOSUPERUSER + NOBYPASSRLS + LOGIN" ok \
    || check "role posture" fail "got '$posture'"

# 2) Role can SELECT
if [ -z "$GRAFANA_HATCHET_PASSWORD" ]; then
    check "datasource credentials" fail "no GRAFANA_HATCHET_READONLY_PASSWORD in .env"
else
    sel=$(docker exec -e PGPASSWORD="$GRAFANA_HATCHET_PASSWORD" georag-postgresql \
        psql -U grafana_hatchet_readonly -d hatchet -tAc \
        "SELECT count(*) FROM v1_runs_olap LIMIT 1;" 2>&1 | tr -d ' ')
    case "$sel" in
        [0-9]*) check "role can SELECT v1_runs_olap (n=$sel)" ok ;;
        *)      check "role SELECT" fail "$sel" ;;
    esac
fi

# 3) Role CANNOT write
ins=$(docker exec -e PGPASSWORD="$GRAFANA_HATCHET_PASSWORD" georag-postgresql \
    psql -U grafana_hatchet_readonly -d hatchet -tAc \
    "INSERT INTO v1_runs_olap (tenant_id, id, kind, workflow_id, workflow_version_id) VALUES ('00000000-0000-0000-0000-000000000000'::uuid, 1, 'TASK', '00000000-0000-0000-0000-000000000000'::uuid, '00000000-0000-0000-0000-000000000000'::uuid);" 2>&1)
case "$ins" in
    *permission*denied*|*ERROR*) check "role CANNOT INSERT v1_runs_olap (read-only enforced)" ok ;;
    *)                            check "read-only enforcement" fail "INSERT did not fail: $ins" ;;
esac

# 4) Datasource YAML present
[ -f "$REPO/docker/grafana/provisioning/datasources/hatchet.yml" ] \
    && check "Hatchet datasource provisioning YAML present" ok \
    || check "datasource YAML" fail "missing"

# 5) Dashboard JSON valid
DASH="$REPO/docker/grafana/dashboards/georag-integrations.json"
if [ ! -f "$DASH" ]; then
    check "Integrations dashboard JSON present" fail "missing"
else
    if python3 -c "import json; json.load(open('$DASH'))" 2>/dev/null; then
        check "Integrations dashboard JSON parses" ok
    else
        check "dashboard JSON" fail "invalid JSON"
    fi
fi

# 6) Dashboard structure
struct_ok=$(python3 -c "
import json
d = json.load(open('$DASH'))
ok = (
    d.get('uid') == 'georag-integrations'
    and len(d.get('panels', [])) >= 4
    and all(name in json.dumps(d) for name in
            ('public_geoscience_pull','external_notification','phase2_smoke'))
)
print('OK' if ok else 'FAIL')
" 2>/dev/null)
[ "$struct_ok" = "OK" ] \
    && check "Dashboard has uid + 4 panels + all 3 flow names" ok \
    || check "dashboard structure" fail "got '$struct_ok'"

# 7) Each rawSql in the dashboard runs against hatchet without error.
sql_ok=$(python3 -c "
import json, subprocess, sys
d = json.load(open('$DASH'))
queries = []
for p in d['panels']:
    for t in p.get('targets', []):
        sql = t.get('rawSql')
        if sql: queries.append(sql)
fails = 0
for sql in queries:
    out = subprocess.run(
        ['docker','exec','-i','georag-postgresql','psql','-U','grafana_hatchet_readonly','-d','hatchet','-tAc', sql],
        env={'PATH':'/usr/bin:/bin','PGPASSWORD':'$GRAFANA_HATCHET_PASSWORD'},
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode != 0 or 'ERROR' in out.stderr:
        fails += 1
        sys.stderr.write(out.stderr)
print(f'{len(queries) - fails}/{len(queries)}')
" 2>/dev/null)
case "$sql_ok" in
    *'/'*)
        n_ok=$(echo "$sql_ok" | cut -d/ -f1)
        n_total=$(echo "$sql_ok" | cut -d/ -f2)
        if [ "$n_ok" = "$n_total" ] && [ "$n_total" -ge 4 ] 2>/dev/null; then
            check "All $n_total dashboard SQL queries run cleanly" ok
        else
            check "dashboard SQL" fail "$sql_ok"
        fi
        ;;
    *)
        check "dashboard SQL" fail "got '$sql_ok'"
        ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo
echo 'NOTE: Live dashboard rendering also requires `docker compose'
echo '  --profile dev-monitor up -d grafana`. The provisioning files'
echo '  are auto-loaded on Grafana start.'
echo

exit $((PASS == TOTAL ? 0 : 1))
