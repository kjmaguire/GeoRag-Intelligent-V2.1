#!/usr/bin/env bash
# =============================================================================
# scripts/lib/redis_password.sh
#
# One place that answers "what is the Redis password?", so no script ever
# needs to write the answer down.
#
# WHY THIS EXISTS. On 2026-08-21 a live Postgres password was found in 25
# tracked files of a then-public repository. ee853f5 swept the Redis password
# out of two of them and MISSED two more, where it survived until 2026-09-15:
#
#     scripts/phase21_step1_verify.sh   REDIS_PWD='<literal>'
#     scripts/phase0_wrapper_smoke.sh   redis-cli -a '<literal>'
#
# Neither was caught by scripts/check-no-committed-secrets.php, for two
# separate reasons, both now fixed there: the name `REDIS_PWD` abbreviates
# past a pattern that matches PASSWORD|PASSWD, and `redis-cli -a '…'` is not
# an assignment at all so no pattern examined it. The checker reported clean
# across 3144 tracked files while sitting on top of both.
#
# The durable fix is not a better regex — it is that a correct script has no
# reason to contain the value. Source this and call `redis_password`.
#
# Resolution order, first hit wins:
#   1. $REDIS_PASSWORD already exported  (compose/CI inject it; container mode)
#   2. $ENV_FILE                          (callers that point at another tree)
#   3. <repo root>/.env                   (the ordinary host-mode case)
#
# Absence is LOUD. An empty password is not a fallback: redis-cli would
# happily send `AUTH ""`, the server would reject it, and the caller would
# report a connection failure rather than a missing credential — which is
# the absence-as-success shape this repository keeps having to dig out.
# =============================================================================

# shellcheck shell=bash

_redis_password_repo_root() {
    # BASH_SOURCE, not $0: this must resolve from the library's own location
    # regardless of which script sourced it or from where it was invoked.
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

redis_password() {
    if [ -n "${REDIS_PASSWORD:-}" ]; then
        printf '%s' "$REDIS_PASSWORD"
        return 0
    fi

    local envfile="${ENV_FILE:-$(_redis_password_repo_root)/.env}"
    if [ ! -r "$envfile" ]; then
        echo "redis_password: no REDIS_PASSWORD in the environment and ${envfile} is not readable." >&2
        echo "                Export REDIS_PASSWORD, or point ENV_FILE at a .env that has it." >&2
        return 1
    fi

    local value
    # cut -f2- so a password containing '=' survives; head -1 so a duplicated
    # key resolves the same way a shell sourcing the file would (first wins is
    # wrong for `source`, but these files have one entry and a second would be
    # a bug worth failing on elsewhere, not silently picking between here).
    value="$(grep -E '^REDIS_PASSWORD=' "$envfile" | head -1 | cut -d= -f2-)"
    # Strip one layer of surrounding quotes if the .env carries them.
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"

    if [ -z "$value" ]; then
        echo "redis_password: ${envfile} has no non-empty REDIS_PASSWORD." >&2
        return 1
    fi

    printf '%s' "$value"
}

export -f redis_password _redis_password_repo_root 2>/dev/null || true
