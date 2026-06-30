<?php

declare(strict_types=1);
use App\Http\Controllers\Admin\ShadowRunsController;
use Illuminate\Contracts\Console\Kernel;

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

// 1. Controller class loads
$c = new ShadowRunsController;
echo 'controller_class='.get_class($c).PHP_EOL;

// 2. Routes registered
$routes = app('router')->getRoutes();
$found = [];
foreach ($routes as $r) {
    $uri = $r->uri();
    if (str_starts_with($uri, 'admin/shadow-runs')) {
        $found[] = strtoupper($r->methods()[0]).' '.$uri.' -> '.$r->getName();
    }
}
foreach ($found as $f) {
    echo 'route: '.$f.PHP_EOL;
}
echo 'route_count='.count($found).PHP_EOL;

// 3. silver.shadow_runs accessible (read-only)
$cnt = DB::connection('pgsql')->table('silver.shadow_runs')->count();
echo 'shadow_runs_total='.$cnt.PHP_EOL;
