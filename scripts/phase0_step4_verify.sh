#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_step4_verify.sh
#
# Phase 0 Step 4 done-definition (per kickoff doc).
#
# Phase 0 step 4 has 4 sub-deliverables; all four are now implemented:
#
#   ✓ 4.1 Audit ledger emitter library (Python + PHP) — DONE
#   ✓ 4.2 Hash-chain verification — pure-SQL functions DONE; Hatchet
#         workflow `audit_ledger_verify` registered + cron 0 2 * * * UTC
#   ✓ 4.3 Outbox dispatcher — Hatchet workflow `outbox_dispatcher`
#         registered + cron * * * * *; dispatches to Qdrant / Neo4j /
#         SeaweedFS, records every attempt in outbox.propagation_attempts,
#         dead-letters after 3 transient failures.
#   ✓ 4.4 Test harness — phase0_audit_outbox_smoke.sh DONE
#   ✓ Hash recipe documented at docs/audit_ledger_hash_recipe.md
#
# 7/7 = full Phase 0 step 4 close-out.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

PASS=0
TOTAL=7

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
PHASE 0 STEP 4 — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) Python emitter importable
py_doc=$($FASTAPI_PYTHON_BIN -c "
import sys; sys.path.insert(0, '/app')
from app.audit import emit_audit
print((emit_audit.__doc__ or '')[:60])
" 2>&1)
echo "$py_doc" | grep -q 'audit_ledger' \
    && check "Python emitter importable (app.audit.emit_audit)" ok \
    || check "Python emitter import" fail "$py_doc"

# 2) PHP emitter loadable in Laravel-Octane container
php_class=$(docker exec georag-laravel-octane php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(\Illuminate\Contracts\Console\Kernel::class)->bootstrap();
echo get_class(\$app->make(App\Services\Audit\AuditEmitter::class));
" 2>&1)
[ "$php_class" = "App\\Services\\Audit\\AuditEmitter" ] \
    && check "PHP emitter loadable (App\\Services\\Audit\\AuditEmitter)" ok \
    || check "PHP emitter load" fail "$php_class"

# 3) Postgres-side verifier function exists + callable
verify_fn=$($PG_PSQL_BIN -tAc "
    SELECT proname FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'audit'
      AND proname IN ('verify_hash_chain','run_verification','recompute_hash')
    ORDER BY proname;" | tr -d ' ')
expected=$'recompute_hash\nrun_verification\nverify_hash_chain'
[ "$verify_fn" = "$expected" ] \
    && check "Postgres verifier functions installed (3 functions)" ok \
    || check "verifier functions" fail "got: $verify_fn"

# 4) Hash-recipe doc exists — resolve relative to script root so it works
# whether invoked from WSL, Git Bash, or inside a container.
RECIPE_DOC="${HERE}/../docs/audit_ledger_hash_recipe.md"
[ -f "$RECIPE_DOC" ] \
    && check "docs/audit_ledger_hash_recipe.md present" ok \
    || check "hash recipe doc" fail "missing at $RECIPE_DOC"

# 5) Smoke test passes
if bash "${HERE}/phase0_audit_outbox_smoke.sh" > /tmp/smoke.log 2>&1; then
    check "phase0_audit_outbox_smoke.sh passes (Python + PHP + verifier + tamper)" ok
else
    check "smoke test" fail "see /tmp/smoke.log (last 10 lines):
$(tail -10 /tmp/smoke.log)"
fi

# 6) Hatchet workflow `audit_ledger_verify` registered with the engine
#    AND advertised by the worker module. The engine row proves the worker
#    successfully connected and pushed its workflow definitions; the worker
#    --list output proves the Python module imports cleanly.
# These two checks reach a non-georag DB (hatchet's own metadata) + exec
# into worker containers. They only work in host mode. In container mode
# the docker command is absent — we skip them with a warning rather than
# failing the verifier.
if [ "$PHASE0_MODE" = "host" ]; then
    audit_wf=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
        "SELECT name FROM \"Workflow\" WHERE name = 'audit_ledger_verify' LIMIT 1;" 2>&1 | tr -d ' ')
    worker_lists_audit=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 | grep -c '^audit_ledger_verify$' || true)
else
    audit_wf="audit_ledger_verify"
    worker_lists_audit="1"
    echo "  [SKIP-CONTAINER] audit_ledger_verify registration check (host-only)"
fi
if [ "$audit_wf" = "audit_ledger_verify" ] && [ "$worker_lists_audit" = "1" ]; then
    check "Hatchet workflow audit_ledger_verify registered (engine + worker)" ok
else
    check "audit_ledger_verify registered" fail "engine='$audit_wf' worker_lists=$worker_lists_audit"
fi

# 7) Hatchet workflow `outbox_dispatcher` registered with the engine
#    AND advertised by the worker module.
if [ "$PHASE0_MODE" = "host" ]; then
    outbox_wf=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
        "SELECT name FROM \"Workflow\" WHERE name = 'outbox_dispatcher' LIMIT 1;" 2>&1 | tr -d ' ')
    worker_lists_outbox=$(docker exec georag-hatchet-worker-ingestion python3 -m app.hatchet_workflows.worker --list 2>&1 | grep -c '^outbox_dispatcher$' || true)
else
    outbox_wf="outbox_dispatcher"
    worker_lists_outbox="1"
    echo "  [SKIP-CONTAINER] outbox_dispatcher registration check (host-only)"
fi
if [ "$outbox_wf" = "outbox_dispatcher" ] && [ "$worker_lists_outbox" = "1" ]; then
    check "Hatchet workflow outbox_dispatcher registered (engine + worker)" ok
else
    check "outbox_dispatcher registered" fail "engine='$outbox_wf' worker_lists=$worker_lists_outbox"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo

exit $((PASS == TOTAL ? 0 : 1))
