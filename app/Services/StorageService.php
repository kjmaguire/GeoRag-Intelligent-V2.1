<?php

declare(strict_types=1);

namespace App\Services;

use DateTimeInterface;
use Illuminate\Contracts\Filesystem\Filesystem;
use Illuminate\Support\Facades\Storage;

/**
 * Thin façade over the three S3-compatible Flysystem disks declared in
 * config/filesystems.php, so call sites reference a named accessor instead
 * of a magic disk-name string (`Storage::disk('s3-bronze')` one typo away
 * from silently hitting the wrong bucket).
 *
 * Storage-abstraction plan PR7 — companion to the Python-side
 * georag_object_storage package; same motivation, scoped down to what
 * Laravel actually needs. Flysystem's own S3 driver already does the
 * client-construction/vendor-neutrality job georag_object_storage does
 * for Python, so this stays a thin disk-selection wrapper, not a new
 * client implementation.
 *
 * Deliberately untyped return/parameter for the disk itself (no
 * `Filesystem` type hint): `Storage::disk()` is called through Laravel's
 * facade `__callStatic`, which PHP never type-checks at the call site —
 * existing tests (UploadVendorProfileTest) exploit exactly that by
 * mocking `Storage::disk('s3')` to return a duck-typed proxy object that
 * doesn't formally implement `Illuminate\Contracts\Filesystem\Filesystem`.
 * A strict `: Filesystem` return/parameter type here would enforce a
 * contract the facade itself never enforced, breaking that mock with a
 * TypeError. PHPDoc still documents the real type for IDEs/static analysis.
 */
class StorageService
{
    /**
     * The primary bronze-bucket disk — read/write. Used for initial
     * upload landing and bronze-prefix listing.
     *
     * @return Filesystem
     */
    public function bronze(): mixed
    {
        return Storage::disk('s3');
    }

    /**
     * Read-only alias onto the same bronze bucket, used for listing and
     * minting presigned URLs. Application code never writes through this
     * disk — see config/filesystems.php's own comment on the 's3-bronze'
     * disk.
     *
     * @return Filesystem
     */
    public function bronzeReadOnly(): mixed
    {
        return Storage::disk('s3-bronze');
    }

    /**
     * The dedicated exports-bucket disk — generated ZIP/CSV/GeoPackage
     * bundles, kept separate from the bronze layer so exports never
     * pollute the immutable raw archive.
     *
     * @return Filesystem
     */
    public function exports(): mixed
    {
        return Storage::disk('s3-exports');
    }

    /**
     * Mint a presigned download URL, defaulting to a 24-hour TTL — the
     * value every existing call site already used inline.
     *
     * @param Filesystem $disk
     */
    public function presignedUrl(mixed $disk, string $key, ?DateTimeInterface $expiresAt = null): string
    {
        return $disk->temporaryUrl($key, $expiresAt ?? now()->addHours(24));
    }
}
