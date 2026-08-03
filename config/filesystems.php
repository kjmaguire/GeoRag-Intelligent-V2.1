<?php

return [
    /*
    |--------------------------------------------------------------------------
    | Default Filesystem Disk
    |--------------------------------------------------------------------------
    |
    | Here you may specify the default filesystem disk that should be used
    | by the framework. The "local" disk, as well as a variety of cloud
    | based disks are available to your application for file storage.
    |
    */

    'default' => env('FILESYSTEM_DISK', 'local'),

    /*
    |--------------------------------------------------------------------------
    | Filesystem Disks
    |--------------------------------------------------------------------------
    |
    | Below you may configure as many filesystem disks as necessary, and you
    | may even configure multiple disks for the same driver. Examples for
    | most supported storage drivers are configured here for reference.
    |
    | Supported drivers: "local", "ftp", "sftp", "s3"
    |
    */

    'disks' => [

        'local' => [
            'driver' => 'local',
            'root' => storage_path('app/private'),
            'serve' => true,
            'throw' => false,
            'report' => false,
        ],

        'public' => [
            'driver' => 'local',
            'root' => storage_path('app/public'),
            'url' => rtrim(env('APP_URL', 'http://localhost'), '/').'/storage',
            'visibility' => 'public',
            'throw' => false,
            'report' => false,
        ],

        's3' => [
            // STORAGE_BACKEND mirrors georag_object_storage's Python-side seam
            // (factory.py): "s3_compatible" (SeaweedFS/MinIO/AWS, default) or
            // "azure_blob". Same env var, same two values, both layers switch
            // together.
            'driver' => env('STORAGE_BACKEND') === 'azure_blob' ? 'azure' : 's3',
            'key' => env('AWS_ACCESS_KEY_ID'),
            'secret' => env('AWS_SECRET_ACCESS_KEY'),
            'region' => env('AWS_DEFAULT_REGION'),
            'bucket' => env('AWS_BUCKET'),
            'url' => env('AWS_URL'),
            // AWS_ENDPOINT_URL is the canonical name georag_object_storage's
            // Python side reads first (storage-abstraction plan); AWS_ENDPOINT
            // is Laravel's own long-standing name for the same value. Read the
            // canonical name first so a deployment that only sets
            // AWS_ENDPOINT_URL doesn't leave PHP pointed at the wrong endpoint.
            'endpoint' => env('AWS_ENDPOINT_URL', env('AWS_ENDPOINT')),
            'use_path_style_endpoint' => env('AWS_USE_PATH_STYLE_ENDPOINT', false),
            'throw' => false,
            'report' => false,
            // Azure Blob — only read when driver resolves to 'azure' above.
            // Container name matches georag_object_storage's azure_config.py
            // Bucket.BRONZE mapping (AZURE_STORAGE_CONTAINER_BRONZE, default
            // "bronze") since this disk is where UploadController streams
            // report/archive uploads (bronze bucket, `reports/{project_id}/…`
            // prefix).
            'connection_string' => env('AZURE_STORAGE_CONNECTION_STRING'),
            'container' => env('AZURE_STORAGE_CONTAINER_BRONZE', 'bronze'),
        ],

        // Read-only access to the bronze bucket — used to mint presigned
        // download URLs for extracted figures (silver.reports figure manifest
        // PNGs live under bronze://figures/{report_id}/). Application code
        // never writes through this disk; it's only used for temporaryUrl().
        's3-bronze' => [
            'driver' => env('STORAGE_BACKEND') === 'azure_blob' ? 'azure' : 's3',
            'key' => env('AWS_ACCESS_KEY_ID'),
            'secret' => env('AWS_SECRET_ACCESS_KEY'),
            'region' => env('AWS_DEFAULT_REGION'),
            'bucket' => env('MINIO_BUCKET_BRONZE', 'bronze'),
            'url' => env('AWS_URL'),
            // AWS_ENDPOINT_URL is the canonical name georag_object_storage's
            // Python side reads first (storage-abstraction plan); AWS_ENDPOINT
            // is Laravel's own long-standing name for the same value. Read the
            // canonical name first so a deployment that only sets
            // AWS_ENDPOINT_URL doesn't leave PHP pointed at the wrong endpoint.
            'endpoint' => env('AWS_ENDPOINT_URL', env('AWS_ENDPOINT')),
            'use_path_style_endpoint' => env('AWS_USE_PATH_STYLE_ENDPOINT', false),
            'throw' => false,
            'report' => false,
            'connection_string' => env('AZURE_STORAGE_CONNECTION_STRING'),
            'container' => env('AZURE_STORAGE_CONTAINER_BRONZE', 'bronze'),
        ],

        // Dedicated bucket for generated export artifacts (ZIP, CSV, GeoPackage
        // bundles). Kept separate from the bronze layer so exports never
        // pollute the immutable raw archive.
        's3-exports' => [
            'driver' => env('STORAGE_BACKEND') === 'azure_blob' ? 'azure' : 's3',
            'key' => env('AWS_ACCESS_KEY_ID'),
            'secret' => env('AWS_SECRET_ACCESS_KEY'),
            'region' => env('AWS_DEFAULT_REGION'),
            'bucket' => env('MINIO_BUCKET_EXPORTS', 'georag-exports'),
            'url' => env('AWS_URL'),
            // AWS_ENDPOINT_URL is the canonical name georag_object_storage's
            // Python side reads first (storage-abstraction plan); AWS_ENDPOINT
            // is Laravel's own long-standing name for the same value. Read the
            // canonical name first so a deployment that only sets
            // AWS_ENDPOINT_URL doesn't leave PHP pointed at the wrong endpoint.
            'endpoint' => env('AWS_ENDPOINT_URL', env('AWS_ENDPOINT')),
            'use_path_style_endpoint' => env('AWS_USE_PATH_STYLE_ENDPOINT', false),
            'throw' => false,
            'report' => false,
            'connection_string' => env('AZURE_STORAGE_CONNECTION_STRING'),
            'container' => env('AZURE_STORAGE_CONTAINER_EXPORTS', 'exports'),
        ],

    ],

    /*
    |--------------------------------------------------------------------------
    | Symbolic Links
    |--------------------------------------------------------------------------
    |
    | Here you may configure the symbolic links that will be created when the
    | `storage:link` Artisan command is executed. The array keys should be
    | the locations of the links and the values should be their targets.
    |
    */

    'links' => [
        public_path('storage') => storage_path('app/public'),
    ],

];
