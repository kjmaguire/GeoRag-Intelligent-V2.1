<?php

declare(strict_types=1);
use App\Http\Controllers\Admin\IntegrationsController;
use Illuminate\Contracts\Console\Kernel;

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

// Exercise the new private loadHatchetDurations method via reflection so
// we can confirm it returns a parseable shape (no SQL errors). For
// workflows with no recent runs the result is just an empty array;
// we only assert the call did NOT throw.

$c = new IntegrationsController;
$rc = new ReflectionClass($c);
$m = $rc->getMethod('loadHatchetDurations');
$m->setAccessible(true);

$names = ['phase2_smoke', 'public_geoscience_pull', 'external_notification'];
$placeholders = implode(',', array_fill(0, count($names), '?'));

try {
    $out = $m->invoke($c, $names, $placeholders);
    echo 'ok=true,count='.count($out).PHP_EOL;
    foreach ($out as $name => $row) {
        $p50 = $row['p50'] === null ? 'NULL' : (string) $row['p50'];
        $p95 = $row['p95'] === null ? 'NULL' : (string) $row['p95'];
        echo "duration[$name]: p50=$p50 p95=$p95".PHP_EOL;
    }
} catch (Throwable $e) {
    echo 'ok=false,error='.$e->getMessage().PHP_EOL;
}

// Also exercise the full rollup so we see a per-flow result with the
// duration fields hydrated.
$mAll = $rc->getMethod('loadHatchetRunRollups');
$mAll->setAccessible(true);
try {
    $rollup = $mAll->invoke($c);
    foreach ($rollup as $name => $row) {
        echo "rollup[$name]: completed={$row['completed']} failed={$row['failed']} ".
             'p50='.($row['p50_duration_ms'] ?? 'NULL').
             ' p95='.($row['p95_duration_ms'] ?? 'NULL').PHP_EOL;
    }
} catch (Throwable $e) {
    echo 'rollup_error='.$e->getMessage().PHP_EOL;
}
