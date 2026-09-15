<?php

declare(strict_types=1);

namespace Tests\Feature\AppBoot;

use Tests\TestCase;

/**
 * The three S3 disks must resolve to the buckets the deployment actually
 * creates.
 *
 * ## What this is here to stop happening again
 *
 * On 2026-09-15, one day before the AWS cutover, none of them did.
 *
 * `config/filesystems.php` read its bucket names from `AWS_BUCKET`,
 * `MINIO_BUCKET_BRONZE` and `MINIO_BUCKET_EXPORTS`. docker-compose.yml sets
 * all three. The ECS task definitions set none of them — they set
 * `AWS_BUCKET_BRONZE`, `AWS_BUCKET_BRONZE_RASTER`, `AWS_BUCKET_EXPORTS` and
 * `AWS_BUCKET_BACKUPS`, which is what the Python side reads. So in
 * production:
 *
 *   - `s3`         bucket => null
 *   - `s3-bronze`  bucket => "bronze"          (compose's default)
 *   - `s3-exports` bucket => "georag-exports"  (compose's default)
 *
 * while the real buckets are `georag-bronze-<account-id>` and
 * `georag-exports-<account-id>`.
 *
 * The `s3` one is the worst of the three. `StorageService::bronze()` returns
 * that disk, and `UploadController` writes every uploaded file through it
 * without checking the return value — and all three disks are configured
 * `'throw' => false`, so a failed `put()` returns false rather than raising.
 * The upload would have answered 200, written its `bronze.manifest` row, and
 * dispatched ingestion for an object that was never stored.
 *
 * Nothing would have been red. That is why this is a test and not a comment.
 *
 * ## Why it asserts on resolution rather than on values
 *
 * It sets the environment the ECS task definitions set, rebuilds the config,
 * and checks the disks land on it. Hard-coding bucket names here would just
 * restate config/filesystems.php in a second place.
 */
final class ObjectStorageBucketResolutionTest extends TestCase
{
    /** Exactly the object-storage variables deploy/aws/terraform/config.tf sets. */
    private const DEPLOYED = [
        'AWS_BUCKET' => 'georag-bronze-000000000000',
        'AWS_BUCKET_BRONZE' => 'georag-bronze-000000000000',
        'AWS_BUCKET_BRONZE_RASTER' => 'georag-bronze-raster-000000000000',
        'AWS_BUCKET_EXPORTS' => 'georag-exports-000000000000',
        'AWS_BUCKET_BACKUPS' => 'georag-backups-000000000000',
    ];

    /** The compose-only names production does NOT set. */
    private const COMPOSE_ONLY = [
        'MINIO_BUCKET_BRONZE',
        'MINIO_BUCKET_EXPORTS',
    ];

    /**
     * @return array<string, string>
     */
    private function resolveDisksUnderDeployedEnv(): array
    {
        $original = [];

        foreach (self::DEPLOYED as $key => $value) {
            $original[$key] = getenv($key);
            putenv("{$key}={$value}");
            $_ENV[$key] = $value;
        }

        // Production does not set these. If the config still reaches for them
        // first, the assertions below are what notices.
        foreach (self::COMPOSE_ONLY as $key) {
            $original[$key] = getenv($key);
            putenv($key);
            unset($_ENV[$key]);
        }

        try {
            $config = require base_path('config/filesystems.php');

            return [
                's3' => $config['disks']['s3']['bucket'],
                's3-bronze' => $config['disks']['s3-bronze']['bucket'],
                's3-exports' => $config['disks']['s3-exports']['bucket'],
            ];
        } finally {
            foreach ($original as $key => $value) {
                if ($value === false) {
                    putenv($key);
                    unset($_ENV[$key]);

                    continue;
                }
                putenv("{$key}={$value}");
                $_ENV[$key] = $value;
            }
        }
    }

    public function test_every_s3_disk_resolves_to_a_deployed_bucket(): void
    {
        foreach ($this->resolveDisksUnderDeployedEnv() as $disk => $bucket) {
            $this->assertNotNull(
                $bucket,
                "The '{$disk}' disk has no bucket under the environment the ECS "
                .'task definitions actually set. Every write through it fails, '
                ."and because the disk is configured 'throw' => false it fails "
                .'by returning false rather than raising.',
            );

            $this->assertContains(
                $bucket,
                array_values(self::DEPLOYED),
                "The '{$disk}' disk resolved to '{$bucket}', which is not one of "
                .'the buckets deploy/aws/terraform/data.tf creates. It is most '
                .'likely a docker-compose default that survived into '
                .'production — the exact shape of the 2026-09-15 finding.',
            );
        }
    }

    public function test_the_bronze_write_disk_is_the_bronze_bucket(): void
    {
        $resolved = $this->resolveDisksUnderDeployedEnv();

        // StorageService::bronze() returns the 's3' disk, not 's3-bronze'.
        // 's3-bronze' is read-only and exists to mint presigned figure URLs.
        // They must name the same bucket or figures resolve against a bucket
        // uploads never wrote to.
        $this->assertSame(
            self::DEPLOYED['AWS_BUCKET_BRONZE'],
            $resolved['s3'],
            "StorageService::bronze() writes through the 's3' disk. If it is "
            .'not the bronze bucket, every upload lands somewhere nothing '
            .'reads.',
        );

        $this->assertSame(
            $resolved['s3'],
            $resolved['s3-bronze'],
            "The read-only 's3-bronze' disk must name the same bucket the "
            ."'s3' disk writes to, or presigned figure URLs point at a bucket "
            .'with no figures in it.',
        );
    }

    public function test_exports_stay_out_of_the_bronze_bucket(): void
    {
        $resolved = $this->resolveDisksUnderDeployedEnv();

        $this->assertSame(
            self::DEPLOYED['AWS_BUCKET_EXPORTS'],
            $resolved['s3-exports'],
            'Generated exports belong in their own bucket. config/filesystems.php '
            .'says so: kept separate "so exports never pollute the immutable raw '
            .'archive".',
        );

        $this->assertNotSame(
            $resolved['s3'],
            $resolved['s3-exports'],
            'Exports and bronze resolved to the same bucket.',
        );
    }
}
