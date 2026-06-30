<?php

use Illuminate\Support\Str;
use Pdo\Mysql;

return [
    /*
    |--------------------------------------------------------------------------
    | Default Database Connection Name
    |--------------------------------------------------------------------------
    |
    | Here you may specify which of the database connections below you wish
    | to use as your default connection for database operations. This is
    | the connection which will be utilized unless another connection
    | is explicitly specified when you execute a query / statement.
    |
    */

    'default' => env('DB_CONNECTION', 'sqlite'),

    /*
    |--------------------------------------------------------------------------
    | Database Connections
    |--------------------------------------------------------------------------
    |
    | Below are all of the database connections defined for your application.
    | An example configuration is provided for each database system which
    | is supported by Laravel. You're free to add / remove connections.
    |
    */

    'connections' => [

        'sqlite' => [
            'driver' => 'sqlite',
            'url' => env('DB_URL'),
            'database' => env('DB_DATABASE', database_path('database.sqlite')),
            'prefix' => '',
            'foreign_key_constraints' => env('DB_FOREIGN_KEYS', true),
            'busy_timeout' => null,
            'journal_mode' => null,
            'synchronous' => null,
            'transaction_mode' => 'DEFERRED',
        ],

        'mysql' => [
            'driver' => 'mysql',
            'url' => env('DB_URL'),
            'host' => env('DB_HOST', '127.0.0.1'),
            'port' => env('DB_PORT', '3306'),
            'database' => env('DB_DATABASE', 'laravel'),
            'username' => env('DB_USERNAME', 'root'),
            'password' => env('DB_PASSWORD', ''),
            'unix_socket' => env('DB_SOCKET', ''),
            'charset' => env('DB_CHARSET', 'utf8mb4'),
            'collation' => env('DB_COLLATION', 'utf8mb4_unicode_ci'),
            'prefix' => '',
            'prefix_indexes' => true,
            'strict' => true,
            'engine' => null,
            'options' => extension_loaded('pdo_mysql') ? array_filter([
                (PHP_VERSION_ID >= 80500 ? Mysql::ATTR_SSL_CA : PDO::MYSQL_ATTR_SSL_CA) => env('MYSQL_ATTR_SSL_CA'),
            ]) : [],
        ],

        'mariadb' => [
            'driver' => 'mariadb',
            'url' => env('DB_URL'),
            'host' => env('DB_HOST', '127.0.0.1'),
            'port' => env('DB_PORT', '3306'),
            'database' => env('DB_DATABASE', 'laravel'),
            'username' => env('DB_USERNAME', 'root'),
            'password' => env('DB_PASSWORD', ''),
            'unix_socket' => env('DB_SOCKET', ''),
            'charset' => env('DB_CHARSET', 'utf8mb4'),
            'collation' => env('DB_COLLATION', 'utf8mb4_unicode_ci'),
            'prefix' => '',
            'prefix_indexes' => true,
            'strict' => true,
            'engine' => null,
            'options' => extension_loaded('pdo_mysql') ? array_filter([
                (PHP_VERSION_ID >= 80500 ? Mysql::ATTR_SSL_CA : PDO::MYSQL_ATTR_SSL_CA) => env('MYSQL_ATTR_SSL_CA'),
            ]) : [],
        ],

        'pgsql' => [
            'driver' => 'pgsql',
            'url' => env('DB_URL'),
            'host' => env('DB_HOST', '127.0.0.1'),
            'port' => env('DB_PORT', '5432'),
            'database' => env('DB_DATABASE', 'laravel'),
            'username' => env('DB_USERNAME', 'root'),
            'password' => env('DB_PASSWORD', ''),
            'charset' => env('DB_CHARSET', 'utf8'),
            'prefix' => '',
            'prefix_indexes' => true,
            // Override via DB_SEARCH_PATH when a connection needs to see
            // tables in non-public schemas (silver, bronze, public_geo,
            // etc.) — e.g., phpunit.pgsql.xml expands this so migrate:fresh
            // can find and drop tables across every application schema.
            'search_path' => env('DB_SEARCH_PATH', 'public'),
            'sslmode' => env('DB_SSLMODE', 'prefer'),
        ],

        // Dedicated migration connection (added 2026-05-22).
        //
        // Why this exists: phase0 raw SQL ran as `georag` and owns most
        // application tables. The runtime app uses `georag_app` (non-
        // privileged) for defense-in-depth — a SQL injection in Laravel
        // can only SELECT/INSERT/UPDATE under RLS, never DDL. Without a
        // separate connection, `artisan migrate` would either need
        // `GRANT georag TO georag_app` (turning the runtime role into a
        // superuser) or per-migration psql workarounds. This connection
        // closes the gap: migrations run with the owner role's DDL
        // privileges while the runtime keeps its scoped role.
        //
        // Configuration (deploy-time env, not committed to .env):
        //   MIGRATE_DB_HOST     = postgresql (direct, not pgbouncer)
        //   MIGRATE_DB_PORT     = 5432
        //   MIGRATE_DB_USERNAME = georag
        //   MIGRATE_DB_PASSWORD = <same as POSTGRES_PASSWORD>
        //   MIGRATE_DB_CONNECTION = pgsql_migrations
        //
        // The bottom env (MIGRATE_DB_CONNECTION) flips the
        // `migrations.connection` resolver below to this entry. Without
        // it set, migrations run on `pgsql` (i.e. the legacy path) — so
        // this connection is opt-in and zero-impact when unset.
        //
        // Direct (not pgbouncer): pgbouncer's transaction-mode pooling
        // breaks DDL operations that depend on session-level state
        // (CREATE INDEX CONCURRENTLY, SET LOCAL, etc.). Migrations must
        // talk to postgres directly.
        'pgsql_migrations' => [
            'driver' => 'pgsql',
            'url' => env('MIGRATE_DB_URL'),
            'host' => env('MIGRATE_DB_HOST', env('POSTGRES_DIRECT_HOST', 'postgresql')),
            'port' => env('MIGRATE_DB_PORT', env('POSTGRES_DIRECT_PORT', '5432')),
            'database' => env('DB_DATABASE', 'georag'),
            'username' => env('MIGRATE_DB_USERNAME', 'georag'),
            'password' => env('MIGRATE_DB_PASSWORD', ''),
            'charset' => env('DB_CHARSET', 'utf8'),
            'prefix' => '',
            'prefix_indexes' => true,
            'search_path' => env('DB_SEARCH_PATH', 'public,silver,bronze,gold,audit,workflow'),
            'sslmode' => env('DB_SSLMODE', 'prefer'),
        ],

        // Phase 1 Step 7 — read-only view onto the Hatchet engine's own
        // Postgres database (separate role, separate logical DB on the same
        // server). The Hatchet Worker Dashboard reads "Worker" + "WorkflowRun"
        // here. Defaults match the docker-compose service config.
        'pgsql_hatchet' => [
            'driver' => 'pgsql',
            'host' => env('HATCHET_PG_HOST', env('DB_HOST', '127.0.0.1')),
            'port' => env('HATCHET_PG_PORT', env('DB_PORT', '5432')),
            'database' => env('HATCHET_PG_DB', 'hatchet'),
            'username' => env('HATCHET_PG_USER', 'hatchet'),
            'password' => env('HATCHET_PG_PASSWORD', ''),
            'charset' => env('DB_CHARSET', 'utf8'),
            'prefix' => '',
            'prefix_indexes' => true,
            'search_path' => 'public',
            'sslmode' => env('DB_SSLMODE', 'prefer'),
        ],

        // Phase 3 Step 7 — pgsql_activepieces connection removed. The
        // Kestra logical DB + role were dropped via
        // database/raw/phase3/90-activepieces-sunset.sql.

        // Phase 3 Step 6 — read-only view onto Kestra's own Postgres
        // database. Reads the `flows` + `executions` tables to surface
        // which flows exist + their recent run state on /admin/integrations.
        // Same role-isolation pattern as pgsql_hatchet + pgsql_activepieces.
        'pgsql_kestra' => [
            'driver' => 'pgsql',
            'host' => env('KESTRA_PG_HOST_LARAVEL', 'postgresql'),
            'port' => env('KESTRA_PG_PORT_LARAVEL', '5432'),
            'database' => env('KESTRA_PG_DATABASE_LARAVEL', 'kestra'),
            'username' => env('KESTRA_PG_USER_LARAVEL', 'kestra'),
            'password' => env('KESTRA_PG_PASSWORD', ''),
            'charset' => env('DB_CHARSET', 'utf8'),
            'prefix' => '',
            'prefix_indexes' => true,
            'search_path' => 'public',
            'sslmode' => env('DB_SSLMODE', 'prefer'),
        ],

        'sqlsrv' => [
            'driver' => 'sqlsrv',
            'url' => env('DB_URL'),
            'host' => env('DB_HOST', 'localhost'),
            'port' => env('DB_PORT', '1433'),
            'database' => env('DB_DATABASE', 'laravel'),
            'username' => env('DB_USERNAME', 'root'),
            'password' => env('DB_PASSWORD', ''),
            'charset' => env('DB_CHARSET', 'utf8'),
            'prefix' => '',
            'prefix_indexes' => true,
            // 'encrypt' => env('DB_ENCRYPT', 'yes'),
            // 'trust_server_certificate' => env('DB_TRUST_SERVER_CERTIFICATE', 'false'),
        ],

    ],

    /*
    |--------------------------------------------------------------------------
    | Migration Repository Table
    |--------------------------------------------------------------------------
    |
    | This table keeps track of all the migrations that have already run for
    | your application. Using this information, we can determine which of
    | the migrations on disk haven't actually been run on the database.
    |
    */

    'migrations' => [
        // Opt-in via MIGRATE_DB_CONNECTION=pgsql_migrations to run
        // migrations as the phase0 owner role. Unset = legacy behaviour
        // (uses the default `pgsql` connection / `georag_app` role).
        'connection' => env('MIGRATE_DB_CONNECTION'),
        'table' => 'migrations',
        'update_date_on_publish' => true,
    ],

    /*
    |--------------------------------------------------------------------------
    | Redis Databases
    |--------------------------------------------------------------------------
    |
    | Redis is an open source, fast, and advanced key-value store that also
    | provides a richer body of commands than a typical key-value system
    | such as Memcached. You may define your connection settings here.
    |
    */

    'redis' => [

        'client' => env('REDIS_CLIENT', 'phpredis'),

        'options' => [
            'cluster' => env('REDIS_CLUSTER', 'redis'),
            'prefix' => env('REDIS_PREFIX', Str::slug((string) env('APP_NAME', 'laravel')).'-database-'),
            'persistent' => env('REDIS_PERSISTENT', false),
        ],

        'default' => [
            'url' => env('REDIS_URL'),
            'host' => env('REDIS_HOST', '127.0.0.1'),
            'username' => env('REDIS_USERNAME'),
            'password' => env('REDIS_PASSWORD'),
            'port' => env('REDIS_PORT', '6379'),
            'database' => env('REDIS_DB', '0'),
            'max_retries' => env('REDIS_MAX_RETRIES', 3),
            'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
            'backoff_base' => env('REDIS_BACKOFF_BASE', 100),
            'backoff_cap' => env('REDIS_BACKOFF_CAP', 1000),
        ],

        // The `cache` connection routes to a dedicated `redis-cache` instance
        // (allkeys-lru, ephemeral) under the staging/prod compose profile when
        // REDIS_CACHE_HOST is set. Falls back to the default Redis instance in
        // dev. See ops/runbooks/redis-3-instance-rollout.md.
        'cache' => [
            'url' => env('REDIS_URL'),
            'host' => env('REDIS_CACHE_HOST', env('REDIS_HOST', '127.0.0.1')),
            'username' => env('REDIS_CACHE_USERNAME', env('REDIS_USERNAME')),
            'password' => env('REDIS_CACHE_PASSWORD', env('REDIS_PASSWORD')),
            'port' => env('REDIS_CACHE_PORT', env('REDIS_PORT', '6379')),
            'database' => env('REDIS_CACHE_DB', '1'),
            'max_retries' => env('REDIS_MAX_RETRIES', 3),
            'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
            'backoff_base' => env('REDIS_BACKOFF_BASE', 100),
            'backoff_cap' => env('REDIS_BACKOFF_CAP', 1000),
        ],

        // The `queue` connection routes Horizon jobs to a dedicated
        // `redis-queue` instance (noeviction, AOF) under the staging/prod
        // compose profile. Falls back to the default Redis instance in dev so
        // current Horizon dispatch behaviour is unchanged.
        // Activate by setting HORIZON_REDIS_CONNECTION=queue in .env.staging.
        'queue' => [
            'host' => env('REDIS_QUEUE_HOST', env('REDIS_HOST', '127.0.0.1')),
            'username' => env('REDIS_QUEUE_USERNAME', env('REDIS_USERNAME')),
            'password' => env('REDIS_QUEUE_PASSWORD', env('REDIS_PASSWORD')),
            'port' => env('REDIS_QUEUE_PORT', env('REDIS_PORT', '6379')),
            'database' => env('REDIS_QUEUE_DB', '0'),
            'max_retries' => env('REDIS_MAX_RETRIES', 3),
            'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
            'backoff_base' => env('REDIS_BACKOFF_BASE', 100),
            'backoff_cap' => env('REDIS_BACKOFF_CAP', 1000),
        ],

        // The `sessions` connection routes Sanctum session storage to a
        // dedicated `redis-sessions` instance (volatile-lru, AOF) under the
        // staging/prod compose profile. Falls back to default Redis in dev.
        // Activate by setting SESSION_CONNECTION=sessions in .env.staging.
        'sessions' => [
            'host' => env('REDIS_SESSION_HOST', env('REDIS_HOST', '127.0.0.1')),
            'username' => env('REDIS_SESSION_USERNAME', env('REDIS_USERNAME')),
            'password' => env('REDIS_SESSION_PASSWORD', env('REDIS_PASSWORD')),
            'port' => env('REDIS_SESSION_PORT', env('REDIS_PORT', '6379')),
            'database' => env('REDIS_SESSION_DB', '0'),
            'max_retries' => env('REDIS_MAX_RETRIES', 3),
            'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
            'backoff_base' => env('REDIS_BACKOFF_BASE', 100),
            'backoff_cap' => env('REDIS_BACKOFF_CAP', 1000),
        ],

    ],

];
