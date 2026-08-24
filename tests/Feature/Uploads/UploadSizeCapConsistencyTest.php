<?php

declare(strict_types=1);

namespace Tests\Feature\Uploads;

use App\Support\Uploads;
use Tests\TestCase;

/**
 * The upload ceiling must be one number, and it must fit in the container.
 *
 * It was three numbers and a fourth in prose:
 *
 *   config/octane.php   package_max_length  2 GiB   (comment said 100 MB)
 *   UploadController    max:6291456         6 GiB
 *   DrillUploadController max:2097152       2 GiB
 *
 * Two independent failures came out of that. The 6 GiB rule was
 * unreachable, because Swoole refuses the connection at package_max_length
 * before any validator runs — so the branch that returns a friendly 422
 * for an oversized file could not be reached, and callers got a dropped
 * socket instead. And 2 GiB is the ENTIRE memory allocation of
 * laravel-octane-cc, granted per worker across four workers, on an app
 * that runs a single replica behind public ingress.
 *
 * These tests hold the invariant rather than the numbers: whatever the
 * ceiling is, every cap derives from it, and it stays inside the container
 * it has to live in.
 */
final class UploadSizeCapConsistencyTest extends TestCase
{
    /**
     * laravel-octane-cc's memory allocation, in bytes (verified live
     * 2026-08-21). If the container is resized, change this — and read the
     * failure message before changing the assertion below it.
     */
    private const CONTAINER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024;

    /** OCTANE_WORKERS on the live app. Each can buffer a request. */
    private const OCTANE_WORKERS = 4;

    public function test_the_swoole_packet_cap_is_the_upload_ceiling(): void
    {
        $this->assertSame(
            Uploads::maxBytes(),
            (int) config('octane.swoole.options.package_max_length'),
            'Swoole will refuse a body larger than package_max_length before '
            .'any validator runs, so a validation cap above it is unreachable '
            .'and a cap below it lets bytes into the worker that will only be '
            .'rejected later.',
        );
    }

    public function test_the_socket_buffer_matches_the_packet_cap(): void
    {
        $this->assertSame(
            (int) config('octane.swoole.options.package_max_length'),
            (int) config('octane.swoole.options.socket_buffer_size'),
        );
    }

    public function test_every_upload_endpoint_derives_its_rule_from_the_ceiling(): void
    {
        $controllers = [
            'UploadController.php',
            'DrillUploadController.php',
        ];

        foreach ($controllers as $file) {
            $source = file_get_contents(app_path('Http/Controllers/Api/V1/'.$file));

            $this->assertStringContainsString(
                "'max:'.Uploads::maxKilobytes()",
                $source,
                "{$file} must derive its file size rule from App\\Support\\Uploads, "
                .'not hardcode a number. Two hardcoded numbers is how this file '
                .'came to exist.',
            );

            $this->assertDoesNotMatchRegularExpression(
                "/'max:\d{5,}'/",
                $source,
                "{$file} still has a hardcoded five-plus-digit `max:` rule. "
                .'Laravel measures `max` on a file in KILOBYTES, which is the '
                .'unit trap that let 6291456 be annotated "6 GB" and sit next '
                .'to a byte-denominated transport cap without anyone noticing '
                .'they disagreed.',
            );
        }
    }

    public function test_a_single_max_size_request_cannot_exhaust_the_container(): void
    {
        // One in-flight upload must leave the container able to keep serving.
        // The old value made this exactly 1.0 — a single request was allowed
        // to claim 100% of the memory the app had.
        $share = Uploads::maxBytes() / self::CONTAINER_MEMORY_BYTES;

        $this->assertLessThanOrEqual(0.5, $share, sprintf(
            'One max-size upload may claim %.0f%% of laravel-octane-cc\'s memory. '
            .'It runs a single replica behind public ingress, so an OOM there is '
            .'the whole site, and Container Apps kills the container rather than '
            .'returning a 413. Lower GEORAG_MAX_UPLOAD_BYTES or raise the '
            .'container allocation — and if you raise the allocation, update '
            .'CONTAINER_MEMORY_BYTES here so this test keeps meaning something.',
            $share * 100,
        ));
    }

    public function test_the_ceiling_is_documented_against_the_worker_count(): void
    {
        // Not an assertion about safety — four concurrent max-size uploads
        // will still hurt — but a check that the arithmetic is stated where
        // someone raising the limit will read it.
        $source = file_get_contents(app_path('Support/Uploads.php'));

        $this->assertStringContainsString('OCTANE_WORKERS=4', $source);
        $this->assertStringContainsString('2 GiB of memory in total', $source);
        $this->assertSame(
            self::OCTANE_WORKERS,
            4,
            'If OCTANE_WORKERS changed on the live app, the sizing note in '
            .'App\\Support\\Uploads is out of date.',
        );
    }

    public function test_kilobyte_conversion_is_exact(): void
    {
        $this->assertSame(
            Uploads::maxBytes(),
            Uploads::maxKilobytes() * 1024,
            'A lossy conversion would let the validation rule and the '
            .'transport cap drift by up to a kilobyte, which is harmless — '
            .'but it would mean the two are not actually the same number, '
            .'and that is the property under test.',
        );
    }

    public function test_the_environment_override_moves_every_cap_together(): void
    {
        $original = $_ENV['GEORAG_MAX_UPLOAD_BYTES'] ?? null;

        try {
            $_ENV['GEORAG_MAX_UPLOAD_BYTES'] = (string) (128 * 1024 * 1024);
            putenv('GEORAG_MAX_UPLOAD_BYTES='.(128 * 1024 * 1024));

            $this->assertSame(128 * 1024 * 1024, Uploads::maxBytes());
            $this->assertSame(128 * 1024, Uploads::maxKilobytes());
            $this->assertSame('128 MB', Uploads::maxHuman());
        } finally {
            putenv('GEORAG_MAX_UPLOAD_BYTES');
            if ($original === null) {
                unset($_ENV['GEORAG_MAX_UPLOAD_BYTES']);
            } else {
                $_ENV['GEORAG_MAX_UPLOAD_BYTES'] = $original;
            }
        }

        $this->assertSame(Uploads::DEFAULT_MAX_BYTES, Uploads::maxBytes());
    }

    public function test_an_invalid_override_falls_back_rather_than_becoming_zero(): void
    {
        // (int) 'unlimited' is 0, and a zero cap would reject every upload
        // while looking like a deliberate setting.
        try {
            putenv('GEORAG_MAX_UPLOAD_BYTES=unlimited');
            $_ENV['GEORAG_MAX_UPLOAD_BYTES'] = 'unlimited';

            $this->assertSame(Uploads::DEFAULT_MAX_BYTES, Uploads::maxBytes());
        } finally {
            putenv('GEORAG_MAX_UPLOAD_BYTES');
            unset($_ENV['GEORAG_MAX_UPLOAD_BYTES']);
        }
    }
}
