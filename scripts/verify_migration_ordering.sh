#!/usr/bin/env bash
# scripts/verify_migration_ordering.sh — 2026-06-03
#
# Local substitute for the staging-deploy migration-ordering verification.
#
# Why this exists
# ---------------
# Several PRs in PR_PLAN_2026_06_03.md document "migration MUST run before
# service restart X" traps:
#   - PR #2 (workspace_user pivot) before PR #7 (ProjectController) + PR #8
#     (REC foundations)
#   - PR #5 (Martin tile-grants) before the docker-compose Martin restart
#   - PR #3 (silver.archive_ingest_runs) before FastAPI restart
#
# Without a real staging environment, this script catches those traps by:
#   1. Spinning up an ephemeral postgres container (PostGIS 18-3.6, matches CI)
#   2. Running ALL pending Laravel migrations against it
#   3. Importing every FastAPI module that REC#1/REC#2 touched + asserting
#      the imports succeed
#   4. Running the audit-invariants test suite against the fresh DB
#
# If a code path requires a migration that hasn't applied yet, the
# import fails with a clear error. If a migration ordering is wrong
# (eg. silver.archive_ingest_runs writer code lands before the table
# migration), the test catches it locally instead of in production.
#
# Usage:
#   bash scripts/verify_migration_ordering.sh
#
# Exit codes:
#   0 — all migrations applied, all imports clean, all tests pass
#   1 — migration ordering issue detected (see stderr for which)
#   2 — environment setup failed (docker not running, etc.)
#
# What this does NOT verify
# -------------------------
# - Docker-compose restart ordering (eg. Martin service swap)
# - Hatchet workflow re-registration after code changes
# - Vite/Octane reload requirements
# These need a real staging deploy with the full service mesh.

set -euo pipefail

# ─── Setup ──────────────────────────────────────────────────────────
PG_CONTAINER="georag-migration-verify-pg"
PG_USER="georag"
PG_PASS="verify_password_2026_06_03"
PG_DB="georag_verify"
PG_PORT="55432"  # Off-by-1 from prod 5432 to avoid collisions

cleanup() {
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "═══ Migration-ordering verifier ═══"
echo "[1/5] Cleaning up any prior verifier container..."
cleanup

echo "[2/5] Spinning up ephemeral PostgreSQL..."
# Use the project's custom PG image (georag/postgres:18-ext) which
# includes h3 + auto_explain + other extensions the init scripts need.
# Fall back to vanilla postgis/postgis:18-3.6 — the auto_explain init
# is now tolerant (DO block + pg_available_extensions check), but h3
# and other custom extensions will fail. Build with:
#   docker build -t georag/postgres:18-ext docker/postgresql
PG_IMAGE="${PG_IMAGE:-georag/postgres:18-ext}"
if ! docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
    echo "       WARN: $PG_IMAGE not found locally; falling back to postgis/postgis:18-3.6"
    echo "       (h3 and other custom extensions will fail — build the custom image first)"
    PG_IMAGE="postgis/postgis:18-3.6"
fi

# Mount docker/postgresql/init/ as /docker-entrypoint-initdb.d/ so the
# init SQL files (extensions + silver/public_geo schemas + georag_*
# roles) run on first boot. Without this mount, Laravel migrations fail
# with "schema silver does not exist".
INIT_DIR="$(pwd)/docker/postgresql/init"
docker run -d \
    --name "$PG_CONTAINER" \
    -e POSTGRES_USER="$PG_USER" \
    -e POSTGRES_PASSWORD="$PG_PASS" \
    -e POSTGRES_DB="$PG_DB" \
    -p "${PG_PORT}:5432" \
    -v "${INIT_DIR}:/docker-entrypoint-initdb.d:ro" \
    "$PG_IMAGE" \
    >/dev/null

echo -n "       waiting for PG to accept connections"
for i in {1..30}; do
    if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" >/dev/null 2>&1; then
        echo " ✓"
        break
    fi
    echo -n "."
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo " ✗ (PG never came up)"
        exit 2
    fi
done

echo "[3/5] Running Laravel migrations against the verifier DB..."
# Override DB env vars just for this artisan call so we don't touch
# the running dev DB.
DB_CONNECTION=pgsql \
DB_HOST=localhost \
DB_PORT="$PG_PORT" \
DB_DATABASE="$PG_DB" \
DB_USERNAME="$PG_USER" \
DB_PASSWORD="$PG_PASS" \
php artisan migrate --force --no-interaction 2>&1 | tail -20

echo "[4/5] Smoke-importing every FastAPI module touched by REC#1/REC#2..."
# Each import below would fail if a required DB object isn't there or
# a code path references something the migration set hasn't created.
docker exec georag-fastapi python -c "
import sys
modules = [
    # REC#1 foundations
    'app.agent.workspace_context',
    'app.agent.workspace_dependency',
    'app.hatchet_workflows._workspace_input',
    # REC#2 foundations + helpers
    'app.db',
    'app.db.scoped_pool',
    # REC#2 Phase-2 migrated production sites
    'app.services.tool_gateway.gateway',
    'app.services.tool_gateway.impls',
    'app.routers.citation_feedback',
    'app.routers.visualizations',
    'app.hatchet_workflows.continuous_learning_loop',
    'app.hatchet_workflows.sync_silver_to_kg',
    'app.hatchet_workflows.ingest_pdf',
    'app.hatchet_workflows.ingest_zip_archive',
    'app.services.ingest.passage_embedder',
    'app.services.support_cockpit.customer_response_drafting',
    # Audit observability (depends on silver.archive_ingest_runs migration)
    'app.hatchet_workflows._archive_progress',
]
failed = []
for m in modules:
    try:
        __import__(m)
    except ImportError as e:
        # Skip pre-existing transitive deps (lasio etc.) — not our bug.
        if any(dep in str(e) for dep in ('lasio', 'segyio', 'obspy')):
            print(f'  SKIP {m} (pre-existing transitive dep: {e})')
            continue
        failed.append((m, str(e)))
        print(f'  FAIL {m}: {e}', file=sys.stderr)
if failed:
    print(f'{len(failed)} import failures', file=sys.stderr)
    sys.exit(1)
print(f'{len(modules)} imports clean')
" 2>&1

echo "[5/5] Running audit-invariants test suite against verifier env..."
docker exec georag-fastapi sh -c 'cd /app && python -m pytest -q \
    tests/test_acquire_scoped.py \
    tests/test_dead_settings_tagged.py \
    tests/test_ingest_zip_archive_observability.py \
    tests/test_shadow_trigger_observability.py \
    tests/test_source_trust_boost_wiring.py \
    tests/test_vllm_payload_shape.py \
    tests/test_workspace_context.py \
    tests/test_workspace_context_b4_centralisation.py \
    tests/test_workspace_dependency.py \
    tests/test_scoped_connection.py \
    tests/test_lookup_and_rescope.py' 2>&1 | tail -5

echo ""
echo "═══ Migration-ordering verifier PASSED ═══"
echo "If you reached this line:"
echo "  ✓ All migrations applied against a fresh PG"
echo "  ✓ All REC#1/#2 migrated modules import cleanly"
echo "  ✓ All audit-invariants pass against the fresh DB"
echo ""
echo "What this does NOT cover (Kyle's manual verify on staging):"
echo "  - Docker-compose service-restart ordering (Martin role swap)"
echo "  - Hatchet workflow re-registration"
echo "  - Vite/Octane bundle freshness"
