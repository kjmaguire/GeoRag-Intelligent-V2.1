<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

// 1. pgsql_kestra connection reachable
try {
    $n_flows = DB::connection('pgsql_kestra')->table('flows')->count();
    echo "kestra_flow_count=" . $n_flows . PHP_EOL;
} catch (\Throwable $e) {
    echo 'kestra_flow_count=ERROR ' . $e->getMessage() . PHP_EOL;
}

// 2. Controller's loadKestraFlows() works
try {
    $c = new App\Http\Controllers\Admin\IntegrationsController();
    $rc = new ReflectionClass($c);
    $m = $rc->getMethod('loadKestraFlows');
    $m->setAccessible(true);
    $out = $m->invoke($c);
    echo 'kestra_helper_count=' . count($out) . PHP_EOL;
    foreach ($out as $row) {
        echo 'kestra_flow: ' . $row['namespace'] . '/' . $row['id'] . ' rev=' . ($row['revision'] ?? 'null') . PHP_EOL;
    }
} catch (\Throwable $e) {
    echo 'kestra_helper_count=ERROR ' . $e->getMessage() . PHP_EOL;
}
