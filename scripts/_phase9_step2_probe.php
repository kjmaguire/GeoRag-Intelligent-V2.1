<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

use App\Http\Controllers\Admin\IntegrationsController;
use App\Models\User;
use Illuminate\Auth\Access\AuthorizationException;
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;

$mode = $argv[1] ?? 'admin';

if ($mode === 'admin') {
    $user = new User;
    $user->id = 999992;
    $user->email = 'phase9-step2-admin@local.test';
    $user->is_admin = true;
} elseif ($mode === 'nonadmin') {
    $user = new User;
    $user->id = 999993;
    $user->email = 'phase9-step2-other@local.test';
    $user->is_admin = false;
} else {
    echo "USAGE: php probe.php admin|nonadmin\n";
    exit(64);
}
Auth::login($user);

$req = Request::create(
    '/admin/integrations/jwt-keys/rotate',
    'POST',
    ['flow_name' => 'phase2_smoke', 'overlap_hours' => 12],
);
$req->headers->set('X-Requested-With', 'XMLHttpRequest');

$controller = new IntegrationsController;

try {
    $response = $controller->rotateFlowKey($req);
    echo 'STATUS='.$response->getStatusCode().PHP_EOL;
    // flash bag is on the session; pull from there
    $flash = session('flash');
    echo 'FLASH='.($flash ?? '').PHP_EOL;
    exit(0);
} catch (AuthorizationException $e) {
    echo 'AUTH_DENIED'.PHP_EOL;
    exit(0);
} catch (Throwable $e) {
    echo 'ERR: '.get_class($e).': '.$e->getMessage().PHP_EOL;
    echo $e->getFile().':'.$e->getLine().PHP_EOL;
    exit(1);
}
