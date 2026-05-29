#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_step5_verify.sh
#
# Phase 0 Step 5 done-definition (per kickoff).
#
# Sub-deliverables status:
#   ✓ 5.1 Agent invocation wrapper (Python decorator + PHP class) — DONE
#   ✓ 5.2 Admin surfaces (timeouts/prompts/pins/workspaces under
#         /admin/agent-config) — DONE (Laravel + Inertia + React)
#   ✓ 5.3 Default rows for the 11 Phase 0 agents — DONE
#   ✓ 5.4 Dry-run sink (workspace.dry_run_outputs writer) — DONE
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

PASS=0
TOTAL=9

# LARAVEL_BASE_URL is the in-cluster URL of the Laravel app for the route
# probes. Inside the docker network, the Octane container is reachable at
# http://laravel-octane:8000 (alias of georag-laravel-octane). Override
# from the host with e.g. LARAVEL_BASE_URL=http://localhost:8081.
LARAVEL_BASE_URL="${LARAVEL_BASE_URL:-http://laravel-octane:80}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

q() {
    $PG_PSQL_BIN -tAc "$1" 2>/dev/null
}

# Probe an admin route. The unauthenticated request is expected to return
# any of: 200 (rendered page on a permissive build), 302 (redirect to
# /login), 401 (auth challenge), 403 (Gate denial). Anything else (404
# from a missing route, 500 from a controller blow-up) is a failure.
probe_admin_route() {
    local label="$1"
    local path="$2"
    local code
    code=$(docker exec georag-laravel-octane sh -c \
        "curl -s -o /dev/null -w '%{http_code}' '${LARAVEL_BASE_URL}${path}'" 2>/dev/null \
        || curl -s -o /dev/null -w '%{http_code}' "${LARAVEL_BASE_URL}${path}" 2>/dev/null)
    case "$code" in
        200|302|401|403)
            check "$label (${path}) returned ${code}" ok
            ;;
        *)
            check "$label (${path})" fail "expected 200/302/401/403, got '${code}'"
            ;;
    esac
}

cat <<'BANNER'

============================================================
PHASE 0 STEP 5 — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) Python decorator importable + has correct signature
py_check=$($FASTAPI_PYTHON_BIN -c "
import sys; sys.path.insert(0, '/app')
from app.agents import georag_agent, AgentContext, AgentResult, register_runtime
import inspect
sig = inspect.signature(georag_agent)
required = {'name', 'risk_tier', 'version'}
have = set(sig.parameters.keys())
print('OK' if required.issubset(have) else f'MISSING:{required - have}')
" 2>&1)
[ "$py_check" = "OK" ] && check "Python @georag_agent decorator importable + signature matches kickoff" ok || check "Python decorator" fail "$py_check"

# 2) PHP AgentInvoker class exists + autoloads
php_check=$(docker exec georag-laravel-octane php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(\Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$cls = new ReflectionClass(App\Services\Agents\AgentInvoker::class);
echo \$cls->getName() . '|' . (\$cls->hasMethod('invoke') ? 'invoke' : 'NOMETHOD');
" 2>&1)
[ "$php_check" = "App\\Services\\Agents\\AgentInvoker|invoke" ] \
    && check "PHP AgentInvoker class loadable + invoke() method present" ok \
    || check "PHP AgentInvoker" fail "$php_check"

# 3) 11 Phase 0 agent_timeouts seed rows present
n_timeouts=$(q "SELECT count(*) FROM workspace.agent_timeouts WHERE agent_name IN (
    'Tenant Isolation Auditor','Lineage Reporter Agent','Storage Tiering Agent',
    'Index Health Agent','Store Reconciliation Agent','Model Upgrade Watch Agent',
    'vLLM Security Check Agent','GPU/VRAM Health Agent','Model Cost Summary Agent',
    'LLM Incident Diagnosis Agent','Support Packet Agent');")
n_timeouts="${n_timeouts// /}"
[ "$n_timeouts" = "11" ] && check "11/11 Phase 0 agent_timeouts seed rows present" ok || check "agent_timeouts seeds" fail "got $n_timeouts / 11"

# 4) 2 LLM-calling Phase 0 agents have prompt pin rows
n_pins=$(q "SELECT count(*) FROM workspace.agent_prompt_pins WHERE agent_name IN (
    'LLM Incident Diagnosis Agent','Support Packet Agent');")
n_pins="${n_pins// /}"
[ "$n_pins" = "2" ] && check "2/2 LLM-agent prompt pin rows present" ok || check "agent_prompt_pins" fail "got $n_pins / 2"

# 5) Wrapper smoke test (5 scenarios — R0 overhead, R2 dedupe, R3 dry-run, timeout, circuit breaker)
if bash ${HERE}/phase0_wrapper_smoke.sh > /tmp/wrapper_smoke.log 2>&1; then
    check "Wrapper smoke (R0 overhead, R2 dedupe, R3 dry-run, timeout, circuit) passes" ok
else
    check "Wrapper smoke" fail "see /tmp/wrapper_smoke.log:
$(tail -10 /tmp/wrapper_smoke.log)"
fi

# 6-9) Phase 0 Step 5.2 — admin agent-config surfaces reachable.
# Each probe is "route exists" (200/302/401/403 = OK; 404/500 = fail).
probe_admin_route "agent-config timeouts surface"  "/admin/agent-config/timeouts"
probe_admin_route "agent-config prompts surface"   "/admin/agent-config/prompts"
probe_admin_route "agent-config pins surface"      "/admin/agent-config/pins"
probe_admin_route "agent-config workspaces surface" "/admin/agent-config/workspaces"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo
echo "PHPUnit feature tests for the four admin surfaces live in:"
echo "  tests/Feature/Admin/AgentConfig/{Timeouts,Prompts,Pins,Workspaces}Test.php"
echo "Run them with the postgres test connection:"
echo "  docker exec georag-laravel-octane php artisan test --compact -c phpunit.pgsql.xml \\"
echo "    --filter='Admin\\\\AgentConfig'"
echo

exit $((PASS == TOTAL ? 0 : 1))
