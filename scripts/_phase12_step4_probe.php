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
$senderId = $argv[2] ?? '';

if ($mode === 'admin') {
    $user = new User;
    $user->id = 999996;
    $user->email = 'phase12-step4-admin@local.test';
    $user->is_admin = true;
} elseif ($mode === 'nonadmin') {
    $user = new User;
    $user->id = 999997;
    $user->email = 'phase12-step4-other@local.test';
    $user->is_admin = false;
} else {
    echo "USAGE: php probe.php admin|nonadmin <sender_id>\n";
    exit(64);
}
Auth::login($user);

$req = Request::create("/admin/integrations/senders/{$senderId}/rotate-hmac", 'POST');
$req->headers->set('X-Requested-With', 'XMLHttpRequest');

$controller = new IntegrationsController;

try {
    $response = $controller->rotateSenderHmac($req, $senderId);
    $session = session();
    $secret = $session->get('sender_secret');
    $flash = $session->get('flash');
    echo 'STATUS='.$response->getStatusCode().PHP_EOL;
    echo 'SECRET_LEN='.(is_string($secret) ? strlen($secret) : 0).PHP_EOL;
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
