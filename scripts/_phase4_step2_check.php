<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';
$app = require '/app/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

echo 'config_user=' . config('services.kestra.basic_auth_user') . PHP_EOL;
echo 'config_pw_len=' . strlen((string) config('services.kestra.basic_auth_password')) . PHP_EOL;

$routes = app('router')->getRoutes();
$count = 0;
foreach ($routes as $r) {
    $uri = $r->uri();
    if (str_starts_with($uri, 'admin/integrations/kestra')) {
        echo 'route: ' . strtoupper($r->methods()[0]) . ' ' . $uri . ' -> ' . ($r->getName() ?? '(anon)') . PHP_EOL;
        $count++;
    }
}
echo 'route_count=' . $count . PHP_EOL;
