<?php

declare(strict_types=1);
use App\Http\Controllers\Admin\IntegrationsController;
use Illuminate\Contracts\Console\Kernel;

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

// 1. Controller class loads
$c = new IntegrationsController;
echo 'controller_class='.get_class($c).PHP_EOL;

// 2. Routes registered
$routes = app('router')->getRoutes();
$found = [];
foreach ($routes as $r) {
    $uri = $r->uri();
    if (str_starts_with($uri, 'admin/integrations')) {
        $found[] = strtoupper($r->methods()[0]).' '.$uri.' -> '.$r->getName();
    }
}
foreach ($found as $f) {
    echo 'route: '.$f.PHP_EOL;
}
echo 'route_count='.count($found).PHP_EOL;

// 3. pgsql_activepieces connection reachable
try {
    $n = DB::connection('pgsql_activepieces')->table('flow')->count();
    echo 'activepieces_flow_count='.$n.PHP_EOL;
} catch (Throwable $e) {
    echo 'activepieces_flow_count=ERROR '.$e->getMessage().PHP_EOL;
}

// 4. Hatchet rollup query runs cleanly (no SQL errors)
try {
    $rows = DB::connection('pgsql_hatchet')->select(
        'SELECT count(*) AS n FROM v1_runs_olap WHERE inserted_at > now() - interval \'24 hours\'',
    );
    echo 'v1_runs_olap_24h='.(int) $rows[0]->n.PHP_EOL;
} catch (Throwable $e) {
    echo 'v1_runs_olap_24h=ERROR '.$e->getMessage().PHP_EOL;
}

// 5. Feature flag rows present (both Activepieces flows)
try {
    $n = DB::connection('pgsql')->table('workspace.feature_flags')
        ->where('flag_name', 'like', 'activepieces.%.enabled')
        ->count();
    echo 'ap_flag_count='.$n.PHP_EOL;
} catch (Throwable $e) {
    echo 'ap_flag_count=ERROR '.$e->getMessage().PHP_EOL;
}
