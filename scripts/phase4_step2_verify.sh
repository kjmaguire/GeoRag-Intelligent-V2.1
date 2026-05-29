#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step2_verify.sh
#
# Phase 4 Step 2 done-definition — Kestra SSO via Sanctum-fronted proxy.
#
#   1. KestraSsoController class loads
#   2. /admin/integrations/kestra/{path?} route registered
#   3. config/services.php has the kestra basic auth keys set
#   4. Unauthenticated proxy call → 403 (Sanctum/Gate denies)
#   5. Admin proxy call → 200/307 (proxies to Kestra successfully)
#   6. Non-admin proxy call → 403 (Gate denies)
#   7. /admin/integrations TSX exposes the "Open Kestra UI" link
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8

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
PHASE 4 STEP 2 — Kestra SSO PROXY VERIFICATION
============================================================
BANNER

# 1) + 2) + 3) — config probe
out=$(docker exec georag-laravel-octane php /app/scripts/_phase4_step2_check.php 2>/dev/null)
user=$(echo "$out" | grep '^config_user=' | cut -d= -f2)
pwlen=$(echo "$out" | grep '^config_pw_len=' | cut -d= -f2)
route_count=$(echo "$out" | grep '^route_count=' | cut -d= -f2)

[ -n "$user" ] && [ "$user" != "" ] \
    && check "config services.kestra.basic_auth_user set ($user)" ok \
    || check "config user" fail "missing"
[ "$pwlen" -ge 8 ] 2>/dev/null \
    && check "config services.kestra.basic_auth_password set (len=$pwlen)" ok \
    || check "config password" fail "got len=$pwlen"
[ "$route_count" = "1" ] \
    && check "admin/integrations/kestra/{path?} route registered" ok \
    || check "route" fail "got $route_count / 1"

# Controller loads (probe inferred — config probe imports the class for routing).
ctrl=$(docker exec georag-laravel-octane php -r '
require "/app/vendor/autoload.php";
$app = require "/app/bootstrap/app.php";
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
echo class_exists("App\\Http\\Controllers\\Admin\\KestraSsoController") ? "OK" : "MISSING";
' 2>&1 | tail -1)
[ "$ctrl" = "OK" ] && check "KestraSsoController class loads" ok \
    || check "controller" fail "$ctrl"

# 4-6) Auth matrix via proxy probe
proxy=$(docker exec georag-laravel-octane php /app/scripts/_phase4_step2_proxy_probe.php 2>&1)
echo "$proxy" | grep -q '^unauthed=403_authz_denied$' \
    && check "Unauthed proxy call → 403 (Sanctum/Gate denies)" ok \
    || check "unauth gate" fail "$(echo "$proxy" | grep unauthed=)"

admin_root=$(echo "$proxy" | grep '^admin_root_status=' | cut -d= -f2)
admin_api=$(echo "$proxy" | grep '^admin_api_search_status=' | cut -d= -f2)
if { [ "$admin_root" = "200" ] || [ "$admin_root" = "307" ]; } && [ "$admin_api" = "200" ]; then
    check "Admin proxy call → 200/307 (Kestra responds via proxy)" ok
else
    check "admin proxy" fail "root=$admin_root api=$admin_api"
fi

echo "$proxy" | grep -q '^nonadmin=403_authz_denied$' \
    && check "Non-admin proxy call → 403 (Gate denies)" ok \
    || check "nonadmin gate" fail "$(echo "$proxy" | grep nonadmin=)"

# 7) UI link present
ui_link=$(docker exec georag-laravel-octane bash -c '
    grep -q "/admin/integrations/kestra/" /app/resources/js/Pages/Admin/Integrations.tsx \
    && echo OK || echo MISSING
')
[ "$ui_link" = "OK" ] \
    && check "Inertia page exposes Open Kestra UI link" ok \
    || check "TSX link" fail "$ui_link"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
