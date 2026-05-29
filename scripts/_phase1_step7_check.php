<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

// 1. Controller class loads
$c = new App\Http\Controllers\Admin\HatchetWorkersController();
echo "controller_class=" . get_class($c) . PHP_EOL;

// 2. Routes registered
$routes = app('router')->getRoutes();
$found = [];
foreach ($routes as $r) {
    $uri = $r->uri();
    if (str_starts_with($uri, 'admin/hatchet-workers')) {
        $found[] = strtoupper($r->methods()[0]) . ' ' . $uri . ' -> ' . $r->getName();
    }
}
foreach ($found as $f) {
    echo "route: " . $f . PHP_EOL;
}
echo "route_count=" . count($found) . PHP_EOL;

// 3. pgsql_hatchet connection reachable + Worker table queryable
try {
    $worker_count = DB::connection('pgsql_hatchet')
        ->table('Worker')
        ->whereNull('deletedAt')
        ->count();
    echo "hatchet_worker_total=" . $worker_count . PHP_EOL;
} catch (\Throwable $e) {
    echo "hatchet_worker_total=ERROR " . $e->getMessage() . PHP_EOL;
}

// 4. Pool rollup helper produces non-empty array
try {
    $rc = new \ReflectionClass($c);
    $m = $rc->getMethod('poolRollup');
    $m->setAccessible(true);
    $pools = $m->invoke($c);
    echo "pool_count=" . count($pools) . PHP_EOL;
    foreach ($pools as $p) {
        echo "pool: " . $p['name'] . " live=" . $p['live'] . " stale=" . $p['stale'] . PHP_EOL;
    }
} catch (\Throwable $e) {
    echo "pool_count=ERROR " . $e->getMessage() . PHP_EOL;
}
