<?php

/*
|--------------------------------------------------------------------------
| Horizon health endpoint
|--------------------------------------------------------------------------
|
| Router script for PHP's built-in server, started alongside the supervisor
| by docker/horizon-entrypoint.sh:
|
|     php -S 0.0.0.0:8080 /app/docker/horizon-health.php
|
| WHY THIS EXISTS
|
| laravel-horizon-cc ran with no liveness or readiness probe, because
| `php artisan horizon` serves no HTTP and Azure Container Apps probes
| speak only HTTP and TCP — there is no exec probe to run
| `horizon:status` with. So a supervisor that had stopped turning kept its
| replica and reported the revision Healthy, and the only symptom was
| queue depth climbing on a dashboard nobody was watching at 03:00.
|
| WHAT IT ACTUALLY CHECKS
|
| Horizon's master supervisor writes a record to Redis on every loop and
| re-sets a 15-second TTL on it (RedisMasterSupervisorRepository::update).
| So `MasterSupervisorRepository::all()` returning nothing means the
| supervisor has not completed a loop in 15 seconds — the wedged case, and
| not observable from outside any other way. This mirrors `horizon:status`.
|
| That distinction matters: a 200 here means Horizon is working, not
| merely that a PHP process is listening. A tcpSocket probe on this port
| would prove only the latter.
|
| TWO ROUTES, BECAUSE PAUSED IS NOT DEAD
|
|   GET /up     liveness  — 200 unless the supervisor stopped looping
|   GET /ready  readiness — 200 only when Horizon is actively processing
|
| `horizon:pause` is a deliberate operator action. If a paused supervisor
| answered 503 on the liveness route, the platform would restart the
| container and silently undo the pause — so paused is 200 on /up and 503
| on /ready. Both routes would be one endpoint if the two probes did not
| mean opposite things about a deliberate state.
|
| WHAT IT DELIBERATELY DOES NOT DO
|
| It does not check queue depth. A deep queue is a capacity problem and
| restarting the container makes it worse; that belongs in an alert, not a
| liveness probe. Only "the supervisor is not running" restarts anything.
|
| For the same reason it does not fail liveness on an unreachable Redis or
| a boot error: a restart cannot reach Redis either, so a dependency
| outage would become a restart loop that additionally kills every job in
| flight. Those answer 503 on /ready and 200 on /up. A boot failure needs
| no help from this file — Horizon is PID 1 in this container, so if the
| framework cannot boot, `php artisan horizon` exits and the platform
| restarts the replica on its own.
|
*/

declare(strict_types=1);

use Illuminate\Contracts\Console\Kernel;
use Illuminate\Foundation\Application;
use Laravel\Horizon\Contracts\MasterSupervisorRepository;

require __DIR__.'/../vendor/autoload.php';

/** @var Application $app */
$app = require __DIR__.'/../bootstrap/app.php';

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';

header('Content-Type: application/json');
header('Cache-Control: no-store');

if (! in_array($path, ['/up', '/ready'], true)) {
    http_response_code(404);
    echo json_encode(['status' => 'no_such_route']);
    exit;
}

try {
    $app->make(Kernel::class)->bootstrap();

    /** @var MasterSupervisorRepository $repository */
    $repository = $app->make(MasterSupervisorRepository::class);

    $masters = $repository->all();

    if (! $masters) {
        // No master heartbeat inside its 15s TTL. Either Horizon never
        // started or it has stopped looping; both warrant a restart.
        http_response_code(503);
        echo json_encode(['status' => 'inactive', 'masters' => 0]);
        exit;
    }

    $paused = count(array_filter(
        $masters,
        static fn ($master): bool => ($master->status ?? null) === 'paused',
    ));

    if ($paused > 0) {
        http_response_code($path === '/ready' ? 503 : 200);
        echo json_encode([
            'status' => 'paused',
            'masters' => count($masters),
            'paused' => $paused,
        ]);
        exit;
    }

    echo json_encode(['status' => 'running', 'masters' => count($masters)]);
} catch (Throwable $e) {
    // Readiness and liveness answer differently here, for the same reason
    // they differ on `paused`.
    //
    // Horizon cannot do useful work without Redis, so /ready is 503 — the
    // platform stops sending it work, which is true and harmless.
    //
    // /up must NOT be, and this is the trap: a 503 on liveness restarts
    // the container. Restarting does not reach Redis, so a Redis outage
    // becomes a restart loop that also kills whatever jobs were in flight
    // — the same "restarting makes it worse" case this file's header
    // already refuses to apply to queue depth. Liveness restarts only
    // what a restart can fix: this process, wedged. That case is the
    // `! $masters` branch above, which is reached only when Redis
    // ANSWERED and had no heartbeat in it. When Redis comes back and
    // Horizon is genuinely dead, that branch fires and the restart
    // happens then.
    //
    // The body carries the exception class only — this response is
    // unauthenticated, and the probe never reads it. The log line is how
    // an operator finds out, since a 200 here is otherwise silent.
    error_log(sprintf(
        'horizon-health: %s on %s: %s',
        $e::class,
        $path,
        $e->getMessage(),
    ));

    http_response_code($path === '/ready' ? 503 : 200);
    echo json_encode(['status' => 'degraded', 'error' => $e::class]);
}
