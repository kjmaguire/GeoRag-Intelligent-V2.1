<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

use App\Http\Controllers\Admin\KestraSsoController;
use App\Models\User;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;

// ─── 1. Unauthed call should NOT reach the proxy ──────────────────────
$request = Request::create('/admin/integrations/kestra/api/v1/flags', 'GET');
$controller = new KestraSsoController();
try {
    $controller->forward($request, 'api/v1/flags');
    echo 'unauthed=200_OR_OTHER_NON_403'.PHP_EOL;
} catch (\Illuminate\Auth\Access\AuthorizationException $e) {
    echo 'unauthed=403_authz_denied'.PHP_EOL;
} catch (\Throwable $e) {
    echo 'unauthed=ERROR '.$e->getMessage().PHP_EOL;
}

// ─── 2. Admin user → proxy returns Kestra response ────────────────────
$admin = new User();
$admin->id = 999999;
$admin->name = 'verifier-admin';
$admin->email = 'verifier-admin@phase4.test';
$admin->is_admin = true;
// Don't ->save() — we don't want to touch the DB.
Auth::login($admin);

// Probe the root path — Kestra returns 307 redirect to /ui.
$request = Request::create('/admin/integrations/kestra/', 'GET');
$response = $controller->forward($request, '');
echo 'admin_root_status='.$response->getStatusCode().PHP_EOL;

// Probe Kestra's tenant-scoped flow search API — auth is injected by
// the proxy, so this should return 200 + a flows JSON array.
$request = Request::create('/admin/integrations/kestra/api/v1/main/flows/search?namespace=', 'GET');
$response = $controller->forward($request, 'api/v1/main/flows/search');
echo 'admin_api_search_status='.$response->getStatusCode().PHP_EOL;
echo 'admin_api_search_body_starts='.substr($response->getContent(), 0, 16).PHP_EOL;

// ─── 3. Non-admin user → AuthorizationException ───────────────────────
$nonAdmin = new User();
$nonAdmin->id = 999998;
$nonAdmin->email = 'non-admin@phase4.test';
$nonAdmin->is_admin = false;
Auth::login($nonAdmin);

$request = Request::create('/admin/integrations/kestra/api/v1/flags', 'GET');
try {
    $controller->forward($request, 'api/v1/flags');
    echo 'nonadmin=200_OR_OTHER_NON_403'.PHP_EOL;
} catch (\Illuminate\Auth\Access\AuthorizationException $e) {
    echo 'nonadmin=403_authz_denied'.PHP_EOL;
} catch (\Throwable $e) {
    echo 'nonadmin=ERROR '.$e->getMessage().PHP_EOL;
}
