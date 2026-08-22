#!/usr/bin/env bash
# =============================================================================
# scripts/ci/dsn_single_source_check.sh
#
# One place builds a Postgres DSN: src/fastapi/app/db/dsn.py.
#
# Until 2026-08-21 there were SIXTY hand-rolled copies — `_dsn()`,
# `_build_dsn()`, `_pg_dsn()`, `_dsn_sync()` — one per Hatchet workflow plus
# several services, each six near-identical lines of os.environ reads. They
# had already drifted:
#
#   * 21 used os.environ.get("POSTGRES_USER", "georag") while 33 used
#     os.environ["POSTGRES_USER"], so the same missing variable produced a
#     clean default in one workflow and a KeyError in the next;
#   * TWO hardcoded ":5432" and ignored POSTGRES_DIRECT_PORT entirely
#     (verbalize_page_images.py, passage_embedder.py);
#   * none percent-encoded the password, so a password containing "@"
#     produced a DSN pointing at a different host;
#   * none appended sslmode.
#
# The sslmode omission was NOT a security hole — georag-pg-cc has
# require_secure_transport=on so asyncpg's default `prefer` already
# negotiates TLS with no plaintext fallback, and `require` does not verify
# certificates either. The cost of the duplication is CHANGE: every future
# connection-level setting (statement_cache_size for PgBouncer,
# application_name for pg_stat_activity attribution, connect_timeout, a
# Hyperdrive DSN) had to be made sixty times, and the one that got missed
# would fail in production at 03:00 during a cron rather than in CI.
#
# This gate keeps them from growing back.
#
# False-positive escape hatch: add  # dsn-single-source-ok: <reason>
#
# Exit 0 = clean. Exit 1 = a new hand-rolled DSN builder appeared.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$HERE"

SCAN="src/fastapi/app"
CANONICAL="src/fastapi/app/db/dsn.py"

FOUND=0

echo "==> DSN single-source check"
echo "    Canonical builder: $CANONICAL"

if [ ! -f "$CANONICAL" ]; then
    echo "  [ERROR] canonical builder is missing: $CANONICAL"
    exit 1
fi

# 1. New DSN-builder function definitions.
defs=$(grep -rnE '^\s*def (_dsn|_build_dsn|_pg_dsn|_dsn_sync|_make_dsn)\s*\(' "$SCAN" \
    --include='*.py' 2>/dev/null \
    | grep -v 'dsn-single-source-ok:' \
    | grep -v '__pycache__' || true)
if [ -n "$defs" ]; then
    echo ""
    echo "  [VIOLATION] hand-rolled DSN builder(s) defined:"
    echo "$defs" | sed 's/^/    /'
    FOUND=$((FOUND + 1))
fi

# 2. Inline postgres:// f-strings assembled from environment reads.
inline=$(grep -rnE 'f"postgres(ql)?://' "$SCAN" --include='*.py' 2>/dev/null \
    | grep -v 'dsn-single-source-ok:' \
    | grep -v '__pycache__' \
    | grep -v "^$CANONICAL:" || true)
if [ -n "$inline" ]; then
    echo ""
    echo "  [VIOLATION] inline DSN f-string(s) — use build_dsn() instead:"
    echo "$inline" | sed 's/^/    /'
    FOUND=$((FOUND + 1))
fi

# 3. POSTGRES_DIRECT_* READ anywhere but the canonical builder and config.
#    Matches an actual read — a quoted env key or a settings attribute —
#    not the many docstrings and comments that name the variable while
#    explaining why background work bypasses the pooler.
direct=$(grep -rnE '["'"'"']POSTGRES_DIRECT_|settings\.POSTGRES_DIRECT_' "$SCAN" --include='*.py' 2>/dev/null \
    | grep -v 'dsn-single-source-ok:' \
    | grep -v '__pycache__' \
    | grep -v "^$CANONICAL:" \
    | grep -v '^src/fastapi/app/config.py:' || true)
if [ -n "$direct" ]; then
    echo ""
    echo "  [VIOLATION] POSTGRES_DIRECT_* read outside the canonical builder:"
    echo "$direct" | sed 's/^/    /'
    FOUND=$((FOUND + 1))
fi

echo
if [ "$FOUND" -eq 0 ]; then
    echo "==> DSN single-source clean — 0 violations"
    exit 0
fi

echo "==> DSN single-source VIOLATED — $FOUND pattern(s) found"
echo "    Import it instead:  from app.db.dsn import build_dsn"
echo "    build_dsn()               -> direct to Postgres (background work)"
echo "    build_dsn(direct=False)   -> via PgBouncer (request path)"
echo "    Or add '# dsn-single-source-ok: <reason>' on the line."
exit 1
