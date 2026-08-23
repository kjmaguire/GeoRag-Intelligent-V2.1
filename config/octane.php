<?php

use App\Services\Azure\RefreshExpiredAzureDisks;
use App\Support\Uploads;
use Laravel\Octane\Contracts\OperationTerminated;
use Laravel\Octane\Events\RequestHandled;
use Laravel\Octane\Events\RequestReceived;
use Laravel\Octane\Events\RequestTerminated;
use Laravel\Octane\Events\TaskReceived;
use Laravel\Octane\Events\TaskTerminated;
use Laravel\Octane\Events\TickReceived;
use Laravel\Octane\Events\TickTerminated;
use Laravel\Octane\Events\WorkerErrorOccurred;
use Laravel\Octane\Events\WorkerStarting;
use Laravel\Octane\Events\WorkerStopping;
use Laravel\Octane\Listeners\CloseMonologHandlers;
use Laravel\Octane\Listeners\CollectGarbage;
use Laravel\Octane\Listeners\DisconnectFromDatabases;
use Laravel\Octane\Listeners\EnsureUploadedFilesAreValid;
use Laravel\Octane\Listeners\EnsureUploadedFilesCanBeMoved;
use Laravel\Octane\Listeners\FlushOnce;
use Laravel\Octane\Listeners\FlushTemporaryContainerInstances;
use Laravel\Octane\Listeners\FlushUploadedFiles;
use Laravel\Octane\Listeners\ReportException;
use Laravel\Octane\Listeners\StopWorkerIfNecessary;
use Laravel\Octane\Octane;

return [
    /*
    |--------------------------------------------------------------------------
    | Octane Server
    |--------------------------------------------------------------------------
    |
    | This value determines the default "server" that will be used by Octane
    | when starting, restarting, or stopping your server via the CLI. You
    | are free to change this to the supported server of your choosing.
    |
    | Supported: "roadrunner", "swoole", "frankenphp"
    |
    */

    'server' => env('OCTANE_SERVER', 'roadrunner'),

    /*
    |--------------------------------------------------------------------------
    | Swoole Server Options
    |--------------------------------------------------------------------------
    |
    | These options are merged over Octane's Swoole defaults. Swoole's own
    | package_max_length default is 10 MB, which rejects geological uploads
    | (LAS, NI 43-101 PDFs, GeoTIFFs) before they reach Laravel.
    |
    | The size comes from App\Support\Uploads, which is also what the
    | `max:` rule on every upload endpoint is derived from — the transport
    | ceiling and the validation ceiling have to agree or one of them is
    | decoration. They did not agree: this read 2 GiB, UploadController
    | allowed 6 GiB (unreachable — Swoole refuses first), and the comment
    | that used to sit here said "Bumped to 100MB". Three numbers and a
    | fourth in prose, none of them the same.
    |
    | 2 GiB was also the entire memory allocation of laravel-octane-cc, per
    | worker, with four workers. See the Uploads docblock for the sizing.
    |
    | GEORAG_MAX_UPLOAD_BYTES moves all of them together. The older
    | OCTANE_MAX_REQUEST_SIZE / OCTANE_SOCKET_BUFFER_SIZE still win when
    | set, so an operator who tuned them keeps their override — but they
    | only move the transport, so setting them without also setting
    | GEORAG_MAX_UPLOAD_BYTES gets you a transport that accepts more than
    | validation will.
    */

    'swoole' => [
        'options' => [
            'package_max_length' => (int) env(
                'OCTANE_MAX_REQUEST_SIZE',
                Uploads::maxBytes(),
            ),
            'socket_buffer_size' => (int) env(
                'OCTANE_SOCKET_BUFFER_SIZE',
                Uploads::maxBytes(),
            ),
        ],
    ],

    /*
    |--------------------------------------------------------------------------
    | Force HTTPS
    |--------------------------------------------------------------------------
    |
    | When this configuration value is set to "true", Octane will inform the
    | framework that all absolute links must be generated using the HTTPS
    | protocol. Otherwise your links may be generated using plain HTTP.
    |
    */

    'https' => env('OCTANE_HTTPS', false),

    /*
    |--------------------------------------------------------------------------
    | Octane Listeners
    |--------------------------------------------------------------------------
    |
    | All of the event listeners for Octane's events are defined below. These
    | listeners are responsible for resetting your application's state for
    | the next request. You may even add your own listeners to the list.
    |
    */

    'listeners' => [
        WorkerStarting::class => [
            EnsureUploadedFilesAreValid::class,
            EnsureUploadedFilesCanBeMoved::class,
        ],

        RequestReceived::class => [
            ...Octane::prepareApplicationForNextOperation(),
            ...Octane::prepareApplicationForNextRequest(),
            // Drop Azure blob disks whose managed-identity bearer token has
            // expired. The SDK takes the token as a constructor string with
            // no way to refresh it, and the FilesystemManager caches the
            // built disk for the worker's whole life — so without this the
            // worker serves 401s on every blob operation from token expiry
            // until it recycles. A single integer comparison on the common
            // path. See App\Services\Azure\AzureBlobDiskLifetime.
            RefreshExpiredAzureDisks::class,
        ],

        RequestHandled::class => [
            //
        ],

        RequestTerminated::class => [
            // 2026-08-11: enabled — this app takes multi-GB uploads; without
            // the flush, long-lived workers accumulate PHP upload temp files
            // until the temp volume fills and uploads 500.
            FlushUploadedFiles::class,
        ],

        TaskReceived::class => [
            ...Octane::prepareApplicationForNextOperation(),
            //
        ],

        TaskTerminated::class => [
            //
        ],

        TickReceived::class => [
            ...Octane::prepareApplicationForNextOperation(),
            //
        ],

        TickTerminated::class => [
            //
        ],

        OperationTerminated::class => [
            FlushOnce::class,
            FlushTemporaryContainerInstances::class,
            // DisconnectFromDatabases::class,
            // CollectGarbage::class,
        ],

        WorkerErrorOccurred::class => [
            ReportException::class,
            StopWorkerIfNecessary::class,
        ],

        WorkerStopping::class => [
            CloseMonologHandlers::class,
        ],
    ],

    /*
    |--------------------------------------------------------------------------
    | Warm / Flush Bindings
    |--------------------------------------------------------------------------
    |
    | The bindings listed below will either be pre-warmed when a worker boots
    | or they will be flushed before every new request. Flushing a binding
    | will force the container to resolve that binding again when asked.
    |
    */

    'warm' => [
        ...Octane::defaultServicesToWarm(),
    ],

    'flush' => [
        //
    ],

    /*
    |--------------------------------------------------------------------------
    | Octane Swoole Tables
    |--------------------------------------------------------------------------
    |
    | While using Swoole, you may define additional tables as required by the
    | application. These tables can be used to store data that needs to be
    | quickly accessed by other workers on the particular Swoole server.
    |
    */

    'tables' => [
        'example:1000' => [
            'name' => 'string:1000',
            'votes' => 'int',
        ],
    ],

    /*
    |--------------------------------------------------------------------------
    | Octane Swoole Cache Table
    |--------------------------------------------------------------------------
    |
    | While using Swoole, you may leverage the Octane cache, which is powered
    | by a Swoole table. You may set the maximum number of rows as well as
    | the number of bytes per row using the configuration options below.
    |
    */

    'cache' => [
        'rows' => 1000,
        'bytes' => 10000,
    ],

    /*
    |--------------------------------------------------------------------------
    | File Watching
    |--------------------------------------------------------------------------
    |
    | The following list of files and directories will be watched when using
    | the --watch option offered by Octane. If any of the directories and
    | files are changed, Octane will automatically reload your workers.
    |
    */

    'watch' => [
        'app',
        'bootstrap',
        'config/**/*.php',
        'database/**/*.php',
        'public/**/*.php',
        'resources/**/*.php',
        'routes',
        'composer.lock',
        '.env',
    ],

    /*
    |--------------------------------------------------------------------------
    | Garbage Collection Threshold
    |--------------------------------------------------------------------------
    |
    | When executing long-lived PHP scripts such as Octane, memory can build
    | up before being cleared by PHP. You can force Octane to run garbage
    | collection if your application consumes this amount of megabytes.
    |
    */

    'garbage' => 50,

    /*
    |--------------------------------------------------------------------------
    | Maximum Execution Time  --  INERT UNDER SWOOLE. READ THIS BEFORE TRUSTING IT.
    |--------------------------------------------------------------------------
    |
    | This key is read ONLY by Octane's FrankenPHP and RoadRunner start
    | commands:
    |
    |   vendor/laravel/octane/src/Commands/StartFrankenPhpCommand.php:237
    |   vendor/laravel/octane/src/Commands/StartRoadRunnerCommand.php:169
    |
    | OCTANE_SERVER is `swoole` in every environment, and StartSwooleCommand
    | sets no equivalent option, so NOTHING reads this value. It has never
    | limited anything. Left in place (rather than deleted) because Octane
    | ships it and a future server switch would need it -- but renamed in
    | spirit by this comment so it stops reading as an active limit.
    |
    | There is no Swoole-side replacement on the version we run. Verified
    | against the live image (Swoole 6.2.1): `max_request_execution_time`,
    | `max_execution_time` and `request_slowlog_timeout` are ALL rejected
    | with `Warning: unsupported option [...]` by Swoole\Server\Helper::
    | checkOptions() -- they existed in Swoole 4.5-5.x and are gone in 6.x.
    | Setting one would move the fiction to a different key, not fix it.
    |
    | So a blocked request is bounded only by:
    |   1. the inner budgets -- HatchetDispatchThrottle::MAX_WAIT_MS (30s,
    |      itself derived from the throttle window) and
    |      services.fastapi.stream_timeout (FASTAPI_STREAM_TIMEOUT, 270s);
    |   2. the Container Apps ingress timeout, which is a platform default
    |      and is not configured on laravel-octane-cc.
    |
    | On a 4-worker, maxReplicas=1 deployment those inner budgets ARE the
    | availability ceiling. Bounding a new blocking call means giving that
    | call its own timeout -- not setting a number here.
    |
    */

    'max_execution_time' => 30,

];
