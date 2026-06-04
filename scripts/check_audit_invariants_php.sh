#!/usr/bin/env bash
# scripts/check_audit_invariants_php.sh — REC#3 (2026-06-03)
#
# Runs the PHP file-content tenancy + frontend audit invariants
# locally before commit. The same set runs in
# .github/workflows/audit-invariants.yml on every PR; this hook
# catches the breakage one round-trip earlier.
#
# Speed budget: under 5 seconds. All assertions are file-content /
# import-time — no DB, no migrations, no network.
#
# Adding a new invariant
# ----------------------
# When you add a regression test to `tests/Feature/Tenancy/` or
# `tests/Feature/Frontend/` that follows the "file-content only,
# no DB" pattern, add its path below AND register it in
# `docs/AUDIT_INVARIANTS.md` + `.github/workflows/audit-invariants.yml`.

set -euo pipefail

PHP_INVARIANTS=(
    "tests/Feature/Tenancy/ArchiveIngestRunsMigrationTest.php"
    "tests/Feature/Tenancy/MartinRoleReadOnlyTest.php"
    "tests/Feature/Tenancy/NoLegacyGucSetConfigInPhpTest.php"
    "tests/Feature/Tenancy/ProjectCreationWorkspaceResolutionTest.php"
    "tests/Feature/Tenancy/WorkspaceActivityChannelAuthTest.php"
    "tests/Feature/Tenancy/WorkspaceRateLimitsTest.php"
    "tests/Feature/Tenancy/WorkspaceUserMembershipTest.php"
    "tests/Feature/Frontend/LegacyChatPageDeletedTest.php"
)

# Verify every entry actually exists. Drift here = a contributor
# deleted a test file but forgot to remove the entry — would silently
# pass with "no tests ran" otherwise.
for f in "${PHP_INVARIANTS[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "audit-invariants: missing test file $f" >&2
        echo "  Either restore it (the bug class it pinned shipped before)" >&2
        echo "  or remove the path from $0 AND docs/AUDIT_INVARIANTS.md." >&2
        exit 1
    fi
done

# Run via artisan test with the file paths. --compact for speed +
# minimal noise on success. Failures surface the full assertion
# messages so the contributor knows what to fix.
php artisan test --compact "${PHP_INVARIANTS[@]}"
