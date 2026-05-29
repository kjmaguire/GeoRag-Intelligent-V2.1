<?php

require_once __DIR__ . '/../vendor/autoload.php';

// PHPUnit's <env force="true"> sets $_ENV and putenv() but does not touch
// $_SERVER. Laravel's vlucas/phpdotenv Env repository reads $_SERVER first,
// so values set in the container's OS environment (e.g. DB_CONNECTION=pgsql
// from docker-compose.yml) win over phpunit.xml overrides — which silently
// routes tests at the real Postgres and breaks RefreshDatabase migrations.
// Mirror $_ENV into $_SERVER so phpunit.xml's forced test values win.
foreach ($_ENV as $phpunitBootstrapKey => $phpunitBootstrapValue) {
    $_SERVER[$phpunitBootstrapKey] = $phpunitBootstrapValue;
}
unset($phpunitBootstrapKey, $phpunitBootstrapValue);
