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
$source = $argv[2] ?? 'phase10-step3-probe';

if ($mode === 'admin') {
    $user = new User;
    $user->id = 999994;
    $user->email = 'phase10-step3-admin@local.test';
    $user->is_admin = true;
} elseif ($mode === 'nonadmin') {
    $user = new User;
    $user->id = 999995;
    $user->email = 'phase10-step3-other@local.test';
    $user->is_admin = false;
} else {
    echo "USAGE: php probe.php admin|nonadmin [source]\n";
    exit(64);
}
Auth::login($user);

$req = Request::create(
    '/admin/integrations/senders',
    'POST',
    [
        'source' => $source,
        'description' => 'phase10 step3 probe sender',
    ],
);
$req->headers->set('X-Requested-With', 'XMLHttpRequest');

$controller = new IntegrationsController;

try {
    $response = $controller->registerSender($req);
    $session = session();
    $secret = $session->get('sender_secret');
    $flash = $session->get('flash');
    echo 'STATUS='.$response->getStatusCode().PHP_EOL;
    echo 'SECRET_LEN='.(is_string($secret) ? strlen($secret) : 0).PHP_EOL;
    echo 'SECRET_PREFIX='.(is_string($secret) ? substr($secret, 0, 8) : '').PHP_EOL;
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
