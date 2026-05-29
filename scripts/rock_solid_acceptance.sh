#!/usr/bin/env bash
# =============================================================================
# scripts/rock_solid_acceptance.sh
#
# Master gate — runs every acceptance harness + every integration-test suite +
# every config sanity check in sequence and reports a single roll-up.
#
# Pre-requisites:
#   - Docker stack up: docker compose ps
#   - FASTAPI_SERVICE_KEY env var set
#   - Node available via Docker (we use node:22-alpine)
#
# Exit 0 = the entire platform passes its safety net. Exit non-zero = at least
# one suite failed.
# =============================================================================

set -uo pipefail

PASS_SUITES=()
FAIL_SUITES=()

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SVC="${FASTAPI_SERVICE_KEY:-$(docker exec georag-fastapi printenv FASTAPI_SERVICE_KEY 2>/dev/null)}"
if [ -z "$SVC" ]; then
    echo "ERROR: FASTAPI_SERVICE_KEY not set + couldn't pull from container"
    exit 2
fi
export FASTAPI_SERVICE_KEY="$SVC"

PG_DSN_C="postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@postgresql:5432/georag"

run_suite() {
    local label="$1"
    local cmd="$2"
    echo
    echo "============================================================"
    echo "  $label"
    echo "============================================================"
    if eval "$cmd"; then
        PASS_SUITES+=("$label")
        echo "  ✓ $label"
    else
        FAIL_SUITES+=("$label")
        echo "  ✗ $label"
    fi
}

cd "$REPO_ROOT"

# ─── Acceptance harnesses (Bash + curl) ─────────────────────────────
run_suite "Phase H4 acceptance"  "bash scripts/phase_h4_acceptance.sh   >/tmp/sh_h4.log    2>&1 && tail -1 /tmp/sh_h4.log"
run_suite "§6 acceptance"        "bash scripts/section6_acceptance.sh   >/tmp/sh_6.log     2>&1 && tail -1 /tmp/sh_6.log"
run_suite "§10 acceptance"       "bash scripts/section10_acceptance.sh  >/tmp/sh_10.log    2>&1 && tail -1 /tmp/sh_10.log"
run_suite "§11-v1 acceptance"    "bash scripts/section11_acceptance.sh  >/tmp/sh_11.log    2>&1 && tail -1 /tmp/sh_11.log"
run_suite "§11-v2 acceptance"    "bash scripts/section11_v2_acceptance.sh >/tmp/sh_11v2.log 2>&1 && tail -1 /tmp/sh_11v2.log"

# ─── New integration test suites ────────────────────────────────────
PYTEST_BASE="MSYS_NO_PATHCONV=1 docker exec -e PG_DSN=$PG_DSN_C -e FASTAPI_SERVICE_KEY=$SVC -e LARAVEL_URL=http://laravel-octane:8000 georag-fastapi python -m pytest"

run_suite "§10.6 promotion gate tests" \
    "$PYTEST_BASE tests/test_promotion_gate.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§10.12 cross-workspace audit tests" \
    "$PYTEST_BASE tests/test_cross_workspace_audit.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§10-v1 tests" \
    "$PYTEST_BASE tests/test_promotion_gate.py tests/test_cross_workspace_audit.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§6.2 wave 3 UPSERT tests" \
    "$PYTEST_BASE tests/test_section6_wave3_upserts.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§11.3 wave 2 export/restore tests" \
    "$PYTEST_BASE tests/test_section11_3_wave2_extras.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§12 what_changed_weekly tests" \
    "$PYTEST_BASE tests/test_what_changed_weekly.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "Demo-ready surface tests" \
    "$PYTEST_BASE tests/test_demo_ready_surfaces.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§4 Tool Gateway tests" \
    "$PYTEST_BASE tests/test_tool_gateway.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"
run_suite "§7.4 Claim Ledger tests" \
    "$PYTEST_BASE tests/test_claim_ledger.py -q -m integration 2>&1 | tail -5 | grep -E '^=+.*(passed|failed)' | tail -1"

# ─── Config sanity ──────────────────────────────────────────────────
run_suite "Vite manifest exists" \
    "test -f public/build/manifest.json && echo 'manifest present'"
run_suite "OpenAPI spec exists + non-trivial" \
    "test -f docs/api/openapi.json && size=\$(wc -c < docs/api/openapi.json) && test \$size -gt 50000 && echo \"openapi.json: \$size bytes\""
run_suite "Tool Gateway: 19 tools registered" \
    "docker exec georag-postgresql psql -U georag -d georag -tAc \"SELECT count(*) FROM workspace.agent_risk_tiers\" | tr -d ' \r' | grep -q '^19\$' && echo '19 tools'"
run_suite "Interpretation schema: 4 tables present" \
    "docker exec georag-postgresql psql -U georag -d georag -tAc \"SELECT count(*) FROM information_schema.tables WHERE table_schema='interpretation'\" | tr -d ' \r' | grep -q '^4\$' && echo '4 tables'"

# ─── Roll-up ────────────────────────────────────────────────────────
echo
echo "============================================================"
echo "  ROLL-UP"
echo "============================================================"
echo "  Passed suites: ${#PASS_SUITES[@]}"
for s in "${PASS_SUITES[@]}"; do echo "    ✓ $s"; done
echo
echo "  Failed suites: ${#FAIL_SUITES[@]}"
for s in "${FAIL_SUITES[@]}"; do echo "    ✗ $s"; done

echo
if [ ${#FAIL_SUITES[@]} -eq 0 ]; then
    echo "  🟢 ROCK SOLID — every suite green."
    exit 0
fi
echo "  🔴 At least one suite failed."
exit 1
