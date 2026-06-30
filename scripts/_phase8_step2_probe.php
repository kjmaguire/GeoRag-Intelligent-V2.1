<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

use App\Http\Controllers\Admin\IntegrationsController;
use App\Models\User;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;

$admin = new User;
$admin->id = 999991;
$admin->email = 'phase8-step2-probe@local.test';
$admin->is_admin = true;
Auth::login($admin);

$req = Request::create('/admin/integrations', 'GET');
// Inertia returns JSON instead of HTML when this header is set,
// which is how we cleanly pluck props in a CLI probe.
$req->headers->set('X-Inertia', 'true');
$controller = new IntegrationsController;

try {
    $response = $controller->index($req);
    $jsonResponse = $response->toResponse($req);
    $payload = json_decode($jsonResponse->getContent(), true);
    $keys = $payload['props']['flow_jwt_keys'] ?? null;
    if (! is_array($keys)) {
        echo 'MISSING_PROP'.PHP_EOL;
        exit(1);
    }
    echo 'rows='.count($keys).PHP_EOL;
    foreach ($keys as $k) {
        if (($k['kid'] ?? '') === 'p8s2-probe') {
            echo 'found='.$k['kid']
               .' flow='.$k['flow_name']
               .' active='.($k['is_active'] ? '1' : '0').PHP_EOL;
            exit(0);
        }
    }
    echo 'NOT_FOUND'.PHP_EOL;
    exit(2);
} catch (Throwable $e) {
    echo 'ERR: '.get_class($e).': '.$e->getMessage().PHP_EOL;
    echo $e->getFile().':'.$e->getLine().PHP_EOL;
    exit(3);
}
