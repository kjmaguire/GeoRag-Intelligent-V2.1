#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step6_verify.sh
#
# Phase 4 Step 6 done-definition — silver.shadow_runs sunset (R-P1-10).
#
#   1. S3 archive present under archive/phase1/shadow_runs/
#   2. silver.shadow_runs table dropped
#   3. Phase 1 feature flags (traffic_pct + shadow_enabled) gone
#   4. ShadowRunsController archived (no longer autoloadable)
#   5. shadow_diff / shadow_diff_scan workflow + classifier sources archived
#   6. AI worker pool no longer advertises shadow_diff / shadow_diff_scan
#   7. /admin/shadow-runs route NOT registered (deep links 404 cleanly)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO=/home/georag/projects/georag

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
PHASE 4 STEP 6 — silver.shadow_runs SUNSET VERIFICATION
============================================================
BANNER

# 1) S3 archive
n_objs=$(docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os, aioboto3
async def main():
    sess = aioboto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name='us-east-1',
    )
    async with sess.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL','http://minio:8333')) as s3:
        resp = await s3.list_objects_v2(Bucket='bronze', Prefix='archive/phase1/shadow_runs/')
        print(resp.get('KeyCount', 0))
asyncio.run(main())
" 2>&1 | tail -1)
[ -n "$n_objs" ] && [ "$n_objs" -ge 1 ] 2>/dev/null \
    && check "S3 archive present under archive/phase1/shadow_runs/ (n=$n_objs)" ok \
    || check "S3 archive" fail "got '$n_objs'"

# 2) Table dropped
table_exists=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT EXISTS(SELECT 1 FROM information_schema.tables
                   WHERE table_schema='silver' AND table_name='shadow_runs');
" 2>/dev/null | tr -d ' ')
[ "$table_exists" = "f" ] \
    && check "silver.shadow_runs dropped" ok \
    || check "table" fail "exists=$table_exists"

# 3) Flags dropped
flag_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workspace.feature_flags
     WHERE flag_name IN ('ingest_pdf_hatchet_traffic_pct','ingest_pdf_shadow_enabled');
" 2>/dev/null | tr -d ' ')
[ "$flag_count" = "0" ] \
    && check "Phase 1 feature flags removed (traffic_pct + shadow_enabled)" ok \
    || check "flag cleanup" fail "got $flag_count"

# 4) Controller archived
if docker exec georag-laravel-octane test -f /app/app/Http/Controllers/Admin/ShadowRunsController.php; then
    check "ShadowRunsController archived" fail "still in active path"
elif docker exec georag-laravel-octane test -f /app/app/Http/Controllers/Admin/_archived/ShadowRunsController.php; then
    check "ShadowRunsController archived under _archived/" ok
else
    check "controller archive" fail "file missing from both active + archive paths"
fi

# 5) Workflow + classifier sources archived
sources_state=$(docker exec georag-hatchet-worker-ai bash -c '
    a=$(test -f /app/app/hatchet_workflows/_archived/shadow_diff.py && echo 1 || echo 0)
    b=$(test ! -f /app/app/hatchet_workflows/shadow_diff.py && echo 1 || echo 0)
    c=$(test -d /app/app/services/_archived/shadow_diff && echo 1 || echo 0)
    d=$(test ! -d /app/app/services/shadow_diff && echo 1 || echo 0)
    echo "$a$b$c$d"
')
[ "$sources_state" = "1111" ] \
    && check "shadow_diff workflow + classifier archived under _archived/" ok \
    || check "source archive" fail "got bitmask '$sources_state' (expected 1111)"

# 6) Worker pool no longer advertises shadow_diff
pool_count=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 \
    | grep -cE '^shadow_diff(_scan)?$' || true)
[ "$pool_count" = "0" ] \
    && check "AI worker no longer advertises shadow_diff / shadow_diff_scan" ok \
    || check "pool advertisement" fail "got $pool_count (expected 0)"

# 7) Route gone
route_count=$(docker exec georag-laravel-octane php -r '
require "/app/vendor/autoload.php";
$app = require "/app/bootstrap/app.php";
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$n = 0;
foreach (app("router")->getRoutes() as $r) {
    if (str_starts_with($r->uri(), "admin/shadow-runs")) $n++;
}
echo $n;
' 2>&1 | tail -1)
[ "$route_count" = "0" ] \
    && check "admin/shadow-runs routes removed (deep links 404)" ok \
    || check "route cleanup" fail "got $route_count (expected 0)"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
