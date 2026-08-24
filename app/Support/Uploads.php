<?php

declare(strict_types=1);

namespace App\Support;

/**
 * The single upload size ceiling.
 *
 * There used to be three, and they did not agree:
 *
 *   - Swoole's `package_max_length` (config/octane.php)  2 GiB
 *   - UploadController's `max:6291456` validation rule   6 GiB
 *   - DrillUploadController's `max:2097152` rule         2 GiB
 *
 * and a comment above the first of them that said "Bumped to 100MB", off
 * by a factor of twenty-one from the line it described.
 *
 * The 6 GiB rule was unreachable. Swoole refuses the connection at
 * `package_max_length` long before Laravel validates anything, so a caller
 * uploading 3 GiB got a dropped connection, never the 422 the rule implies.
 * The rule described a limit the transport would not permit.
 *
 * The 2 GiB figure was worse than unreachable, because laravel-octane-cc is
 * allocated **2 GiB of memory in total** and runs OCTANE_WORKERS=4 plus
 * OCTANE_TASK_WORKERS=6 inside it (verified against the live app,
 * 2026-08-21). Each worker was permitted a request buffer the size of the
 * whole container, on an app with minReplicas=maxReplicas=1 and public
 * ingress — so a single large POST could OOM-kill the only replica serving
 * the site, and not gracefully: no 413, no error page, just a dead
 * container and a restart.
 *
 * 512 MB is the default here because it is the largest figure that leaves
 * the container able to survive one: ~1.5 GiB remains for the ten PHP
 * processes and the OS. It comfortably covers NI 43-101 reports and LAS
 * files, which are what actually arrives. Raise GEORAG_MAX_UPLOAD_BYTES if
 * a deployment needs more and has the memory to back it — the value flows
 * to the Swoole packet cap and to every validation rule from this one
 * place, so it cannot be raised in one and forgotten in the others.
 *
 * Note this is a smaller NUMBER but not a smaller working limit: uploads
 * anywhere near the old caps already failed, they just failed as a dead
 * container rather than as a validation error.
 *
 * The real fix is to stop routing file bytes through the web tier at all —
 * a SAS-signed direct-to-blob upload puts the ceiling on Azure Storage
 * where it belongs, and takes PHP's memory out of the question. That is a
 * frontend change as well as a backend one, so it is not this.
 */
final class Uploads
{
    /**
     * Default ceiling in bytes.
     *
     * Sized against laravel-octane-cc's 2 GiB allocation; see the class
     * docblock for the arithmetic.
     */
    public const DEFAULT_MAX_BYTES = 512 * 1024 * 1024;

    /**
     * Absolute ceiling, in bytes, for a single uploaded file.
     *
     * Read directly from the environment rather than from config() so that
     * config/octane.php — which is itself loaded during config bootstrap —
     * can use it without depending on config file load order.
     */
    public static function maxBytes(): int
    {
        $configured = getenv('GEORAG_MAX_UPLOAD_BYTES');
        if ($configured === false || ! is_numeric($configured)) {
            $configured = $_ENV['GEORAG_MAX_UPLOAD_BYTES'] ?? null;
        }

        if (is_numeric($configured) && (int) $configured > 0) {
            return (int) $configured;
        }

        return self::DEFAULT_MAX_BYTES;
    }

    /**
     * The same ceiling in kilobytes, for Laravel's `max:` validation rule.
     *
     * `max` on a file is measured in KILOBYTES, which is the unit trap that
     * let `max:6291456` read as "6 GB" in a comment while the surrounding
     * config was in bytes. Deriving it removes the conversion from every
     * call site.
     */
    public static function maxKilobytes(): int
    {
        return intdiv(self::maxBytes(), 1024);
    }

    /**
     * A human-readable ceiling for error messages, e.g. "512 MB".
     */
    public static function maxHuman(): string
    {
        $mb = self::maxBytes() / (1024 * 1024);

        return $mb >= 1024
            ? rtrim(rtrim(number_format($mb / 1024, 1), '0'), '.').' GB'
            : rtrim(rtrim(number_format($mb, 1), '0'), '.').' MB';
    }
}
