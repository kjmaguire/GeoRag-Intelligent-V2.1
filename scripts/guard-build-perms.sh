#!/usr/bin/env bash
#
# Guard: verify public/build is owned by the host dev user.
#
# Symptom this prevents:
#   Vite build fails with `EACCES, Permission denied` on `rm -rf public/build/assets`
#   because a previous container run that bind-mounted public/build wrote files as
#   root, and the host dev user can't clean them up without sudo.
#
# Exit 0 → all ownership OK.
# Exit 1 → ownership mismatch; prints the stale files.
#
# Usage:
#   scripts/guard-build-perms.sh           # check + report
#   scripts/guard-build-perms.sh --fix     # chown via the root-running laravel container
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="${ROOT}/public/build"

if [[ ! -d "${BUILD_DIR}" ]]; then
    # No build dir yet — nothing to guard. vite build will create it.
    exit 0
fi

expected_user="$(id -un)"
bad="$(find "${BUILD_DIR}" ! -user "${expected_user}" -print 2>/dev/null | head -20 || true)"

if [[ -z "${bad}" ]]; then
    exit 0
fi

echo "guard-build-perms: found files in ${BUILD_DIR} NOT owned by '${expected_user}':" >&2
echo "${bad}" >&2
echo "" >&2

if [[ "${1:-}" == "--fix" ]]; then
    expected_uid="$(id -u)"
    expected_gid="$(id -g)"
    echo "guard-build-perms: fixing via docker exec -u root georag-laravel-octane..." >&2
    if docker ps --format '{{.Names}}' | grep -q '^georag-laravel-octane$'; then
        docker exec -u root georag-laravel-octane chown -R "${expected_uid}:${expected_gid}" /app/public/build
        echo "guard-build-perms: fixed." >&2
        exit 0
    else
        echo "guard-build-perms: georag-laravel-octane container is not running; bring it up or run sudo chown -R ${expected_uid}:${expected_gid} public/build" >&2
        exit 1
    fi
fi

echo "Run: scripts/guard-build-perms.sh --fix  (requires georag-laravel-octane running)" >&2
echo "Or:  sudo chown -R ${expected_user}:${expected_user} public/build" >&2
exit 1
