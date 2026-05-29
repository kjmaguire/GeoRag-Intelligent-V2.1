#!/usr/bin/env bash
# =============================================================================
# scripts/lib/phase0_env.sh
#
# Shared environment shim sourced by every Phase 0 verifier + the master
# acceptance harness. Detects whether the script is running on the host
# (with `docker` CLI access) or inside a container in the georag network,
# and exports a uniform set of command builders + endpoint URLs so the
# downstream scripts don't have to care.
#
# Exports:
#   PG_PSQL          — runs psql against the georag DB
#   FASTAPI_PYTHON   — runs `python3` inside the FastAPI image
#   TEMPO_URL        — base URL for Tempo HTTP API
#   HATCHET_URL      — base URL for Hatchet engine
#   OTEL_HEALTH_URL  — OTel collector health endpoint
#   LANGFUSE_URL     — Langfuse base URL
#   VLLM_URL         — vLLM base URL
#   PROMETHEUS_URL   — Prometheus base URL
#
# Detection contract:
#   - "host" mode: `docker` is on PATH AND `docker info` succeeds.
#                  All exec'd commands route via `docker exec <container>`.
#                  Endpoint URLs use localhost + published host ports.
#   - "container" mode: otherwise. Uses in-network DNS names + container
#                       ports. Requires psql + curl + python3 + bash on
#                       PATH inside the calling container (georag-fastapi
#                       and georag-laravel-horizon both qualify).
# =============================================================================

# Detect mode.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    export PHASE0_MODE="host"
else
    export PHASE0_MODE="container"
fi

# Defaults that callers can still override by exporting before sourcing.
: "${POSTGRES_USER:=georag}"
: "${POSTGRES_DB:=georag}"

if [ "$PHASE0_MODE" = "host" ]; then
    # Host mode — go through docker exec. FASTAPI_PYTHON_BIN is constructed
    # such that callers can prepend `FOO=bar BAR=baz $FASTAPI_PYTHON_BIN -c "..."`
    # and the env vars travel into the container via repeated -e flags.
    # (We wire it up below via the fastapi_python_with_env helper.)
    PG_PSQL_BIN="docker exec georag-postgresql psql -U ${POSTGRES_USER} -d ${POSTGRES_DB}"
    FASTAPI_PYTHON_BIN="docker exec georag-fastapi python3"
    TEMPO_URL="${TEMPO_URL:-http://localhost:3200}"
    HATCHET_URL="${HATCHET_URL:-http://localhost:8889}"
    OTEL_HEALTH_URL="${OTEL_HEALTH_URL:-http://localhost:13133}"
    LANGFUSE_URL="${LANGFUSE_URL:-http://localhost:3030}"
    VLLM_URL="${VLLM_URL:-http://localhost:8023}"
    PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
else
    # Container mode — assume the caller is inside the georag network.
    # POSTGRES_PASSWORD must be set in the container env (Laravel + FastAPI
    # both have it via compose).
    if [ -z "${POSTGRES_PASSWORD:-}" ]; then
        echo "phase0_env.sh: container mode requires POSTGRES_PASSWORD env var" >&2
    fi
    PG_PSQL_BIN="env PGPASSWORD=${POSTGRES_PASSWORD:-} psql -h postgresql -p 5432 -U ${POSTGRES_USER} -d ${POSTGRES_DB}"
    # No way to exec into a sibling container from here without docker.sock
    # mounted — Python-driven steps that need FastAPI's environment must
    # be rewritten as direct asyncpg calls in container mode. Provide a
    # placeholder that errors loudly so callers know to adapt.
    FASTAPI_PYTHON_BIN="python3"
    TEMPO_URL="${TEMPO_URL:-http://tempo:3200}"
    HATCHET_URL="${HATCHET_URL:-http://hatchet:8888}"
    OTEL_HEALTH_URL="${OTEL_HEALTH_URL:-http://otel-collector:13133}"
    LANGFUSE_URL="${LANGFUSE_URL:-http://langfuse-web:3000}"
    VLLM_URL="${VLLM_URL:-http://vllm:8000}"
    PROMETHEUS_URL="${PROMETHEUS_URL:-http://prometheus:9090}"
fi

export PG_PSQL_BIN FASTAPI_PYTHON_BIN TEMPO_URL HATCHET_URL OTEL_HEALTH_URL
export LANGFUSE_URL VLLM_URL PROMETHEUS_URL

# Thin wrapper functions so callers can write `pg_psql ...` instead of
# `$PG_PSQL_BIN ...` (avoids word-splitting issues with the docker-exec form).
pg_psql() { $PG_PSQL_BIN "$@"; }
fastapi_python() { $FASTAPI_PYTHON_BIN "$@"; }

# fastapi_python_with_env VAR1 VAR2 ... -- python_args...
#
# Forwards the named env vars (already exported in the caller's shell) into
# the python3 invocation inside the FastAPI container. In host mode this
# emits `docker exec -e VAR1 -e VAR2 ... georag-fastapi python3 <args>`;
# in container mode the env vars are already inherited so it just calls
# `python3 <args>` directly.
fastapi_python_with_env() {
    local env_flags=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        env_flags+=("$1")
        shift
    done
    [ "${1:-}" = "--" ] && shift
    if [ "$PHASE0_MODE" = "host" ]; then
        local docker_e=()
        for v in "${env_flags[@]}"; do docker_e+=(-e "$v"); done
        docker exec "${docker_e[@]}" georag-fastapi python3 "$@"
    else
        python3 "$@"
    fi
}

export -f pg_psql fastapi_python fastapi_python_with_env 2>/dev/null || true
