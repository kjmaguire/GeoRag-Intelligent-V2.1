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

$senderId = $argv[1] ?? '';
$overlap = $argv[2] ?? '0';

$user = new User;
$user->id = 999998;
$user->email = 'phase14-step2-admin@local.test';
$user->is_admin = true;
Auth::login($user);

$req = Request::create(
    "/admin/integrations/senders/{$senderId}/rotate-hmac",
    'POST',
    ['overlap_hours' => (int) $overlap],
);
$req->headers->set('X-Requested-With', 'XMLHttpRequest');

$controller = new IntegrationsController;

try {
    $response = $controller->rotateSenderHmac($req, $senderId);
    echo 'STATUS='.$response->getStatusCode().PHP_EOL;
    echo 'FLASH='.(session('flash') ?? '').PHP_EOL;
    exit(0);
} catch (Throwable $e) {
    echo 'ERR: '.get_class($e).': '.$e->getMessage().PHP_EOL;
    exit(1);
}
