#!/bin/sh
# Start Horizon with a health endpoint beside it.
#
# The laravel image is shared by three services that differ only in their
# command (see the header of docker/laravel.Dockerfile). Octane and Reverb
# both serve HTTP and so can be probed directly; `php artisan horizon`
# serves nothing, which is why laravel-horizon-cc had no liveness or
# readiness probe at all and a wedged supervisor kept its replica.
#
# Azure Container Apps probes speak only HTTP and TCP — there is no exec
# probe that could run `horizon:status` — so the health signal has to be
# an HTTP listener inside this container. docker/horizon-health.php is
# that listener; it reports Horizon's own master-supervisor heartbeat
# rather than its own liveness. See its header for the routes.
#
# ORDERING AND SIGNALS
#
# The health server is started first and backgrounded, then Horizon is
# exec'd so that it becomes PID 1 and receives SIGTERM directly from the
# platform. That is load-bearing: Horizon drains in-flight jobs on
# SIGTERM, and a shell sitting in front of it that does not forward
# signals would turn every deploy into a batch of killed jobs.
#
# exec discards any trap, so the health server is not cleaned up here. It
# does not need to be — it dies with the container, and if it somehow
# outlived Horizon the probes would fail and the platform would restart
# the replica, which is the outcome we want anyway.
set -eu

HEALTH_PORT="${HORIZON_HEALTH_PORT:-8080}"
HEALTH_ROUTER="${HORIZON_HEALTH_ROUTER:-/app/docker/horizon-health.php}"

# No listener is not "degraded", it is a restart loop with no explanation:
# laravel-horizon-cc carries a liveness probe on this port, so a Horizon
# that starts without one is killed on the failureThreshold and killed
# again on the next replica, forever, while the logs say only that it
# started. Exiting here costs the same availability and says why.
#
# The router ships in the image (docker/laravel.Dockerfile `COPY . .`), so
# reaching either of these branches means a broken image or a bad
# HORIZON_HEALTH_ROUTER override — both worth stopping for, and neither
# survivable by carrying on.
if [ ! -f "$HEALTH_ROUTER" ]; then
    echo "horizon-entrypoint: $HEALTH_ROUTER missing — refusing to start Horizon" >&2
    echo "horizon-entrypoint: the liveness probe on :${HEALTH_PORT} would restart-loop this replica" >&2
    exit 1
fi

# Access logs go to /dev/null on purpose. A probe every 15 seconds is
# ~5,800 lines a day of "GET /up 200" per replica, against a 2 GB/day cap
# on the workspace. Startup failures are still caught, below.
php -S "0.0.0.0:${HEALTH_PORT}" "$HEALTH_ROUTER" >/dev/null 2>&1 &
HEALTH_PID=$!

# One check that it survived binding the port. Steady state stays silent;
# a port clash or a syntax error in the router would otherwise show up
# only as a restart loop with no explanation in the logs.
sleep 1
if ! kill -0 "$HEALTH_PID" 2>/dev/null; then
    echo "horizon-entrypoint: health endpoint failed to start on :${HEALTH_PORT} — refusing to start Horizon" >&2
    exit 1
fi

echo "horizon-entrypoint: health endpoint on :${HEALTH_PORT} (pid ${HEALTH_PID})"

exec php artisan horizon
