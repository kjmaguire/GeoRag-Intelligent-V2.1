#!/usr/bin/env bash
# Discrimination tests for scripts/check-rls-force-parity.php.
#
# A gate that only ever prints OK against a tree someone has already
# baselined has demonstrated nothing. Each case below plants a DDL file in a
# THROWAWAY copy of the repo's database/ trees and asserts the checker's
# verdict, so a regex that quietly stops recognising `FORCE  ROW LEVEL
# SECURITY` (two spaces — database/raw/phase0/98 is written that way), or
# starts accepting an ENABLE with no partner, fails here.
#
# Run: bash scripts/tests/rls_force_parity_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PASS=0
FAIL=0

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A minimal repo the checker can run against: its own directory layout, an
# empty baseline, and whatever DDL a case plants.
setup() {
    rm -rf "${WORK:?}/repo"
    mkdir -p "$WORK/repo/scripts" "$WORK/repo/database/migrations" "$WORK/repo/database/raw/phase0"
    cp "$REPO_ROOT/scripts/check-rls-force-parity.php" "$WORK/repo/scripts/"
    printf '# empty\n' > "$WORK/repo/scripts/rls-force-baseline.txt"
}

run() { php "$WORK/repo/scripts/check-rls-force-parity.php" 2>&1; }

assert() {
    # assert <label> <expected exit> [substring that must appear]
    local label="$1" want="$2" needle="${3:-}" out code
    out="$(run)"; code=$?
    if [ "$code" != "$want" ]; then
        printf 'FAIL %s -> exit %s, wanted %s\n%s\n' "$label" "$code" "$want" "$out"
        FAIL=$((FAIL + 1)); return
    fi
    if [ -n "$needle" ] && ! printf '%s' "$out" | grep -qF "$needle"; then
        printf 'FAIL %s -> exit %s as expected, but output lacked %s\n%s\n' \
            "$label" "$code" "$needle" "$out"
        FAIL=$((FAIL + 1)); return
    fi
    printf 'ok   %s\n' "$label"
    PASS=$((PASS + 1))
}

# ── 1. An ENABLE with no FORCE, unbaselined, fails and names the table.
setup
cat > "$WORK/repo/database/migrations/2030_01_01_000000_probe.php" <<'PHP'
<?php
DB::statement("ALTER TABLE silver.probe ENABLE ROW LEVEL SECURITY");
PHP
assert "unforced ENABLE fails and names the table" 1 "silver.probe"

# ── 2. The same file with the partner statement passes.
cat >> "$WORK/repo/database/migrations/2030_01_01_000000_probe.php" <<'PHP'
DB::statement("ALTER TABLE silver.probe FORCE ROW LEVEL SECURITY");
PHP
assert "ENABLE plus FORCE passes" 0

# ── 3. Two spaces between FORCE and ROW still counts. database/raw/phase0/98
#      is written that way throughout, and a single-space regex read that file
#      as thirteen unforced tables when this checker was first drafted.
setup
cat > "$WORK/repo/database/raw/phase0/50-probe.sql" <<'SQL'
ALTER TABLE silver.probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.probe FORCE  ROW LEVEL SECURITY;
SQL
assert "FORCE with doubled whitespace counts" 0

# ── 4. An interpolated name matches itself, so a loop that does both passes.
setup
cat > "$WORK/repo/database/migrations/2030_01_01_000000_loop.php" <<'PHP'
<?php
foreach ($tables as $qualified) {
    DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
    DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
}
PHP
assert "interpolated table name pairs with itself" 0

# ── 5. ...and a loop that forgets the partner still fails.
setup
cat > "$WORK/repo/database/migrations/2030_01_01_000000_loop.php" <<'PHP'
<?php
foreach ($tables as $qualified) {
    DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
}
PHP
assert "interpolated ENABLE with no partner fails" 1 '{$qualified}'

# ── 6. FORCING A DIFFERENT TABLE IS NOT COVER. The check is per table, not
#      per file; a file that forces its neighbour and not itself is the
#      copy-paste this gate exists to catch.
setup
cat > "$WORK/repo/database/migrations/2030_01_01_000000_mixed.php" <<'PHP'
<?php
DB::statement("ALTER TABLE silver.one ENABLE ROW LEVEL SECURITY");
DB::statement("ALTER TABLE silver.one FORCE ROW LEVEL SECURITY");
DB::statement("ALTER TABLE silver.two ENABLE ROW LEVEL SECURITY");
PHP
assert "per-table, not per-file" 1 "silver.two"

# ── 7. A baselined entry is tolerated.
setup
cat > "$WORK/repo/database/migrations/2030_01_01_000000_probe.php" <<'PHP'
<?php
DB::statement("ALTER TABLE silver.probe ENABLE ROW LEVEL SECURITY");
PHP
echo 'database/migrations/2030_01_01_000000_probe.php::silver.probe' \
    >> "$WORK/repo/scripts/rls-force-baseline.txt"
assert "a baselined entry passes" 0

# ── 8. ...and once it is closed, the stale baseline line fails.
cat >> "$WORK/repo/database/migrations/2030_01_01_000000_probe.php" <<'PHP'
DB::statement("ALTER TABLE silver.probe FORCE ROW LEVEL SECURITY");
PHP
assert "a closed-but-still-baselined entry fails" 1 "no longer have the gap"

# ── 9. The real repository is green. This is the case that would have caught
#      the drafting error above: the checker reported thirteen phase0/98
#      tables as unforced when they were all forced with doubled whitespace.
cd "$REPO_ROOT"
if php scripts/check-rls-force-parity.php >/dev/null 2>&1; then
    printf 'ok   the repository itself passes\n'; PASS=$((PASS + 1))
else
    printf 'FAIL the repository itself does not pass\n'; FAIL=$((FAIL + 1))
fi

echo
echo "rls force parity: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
