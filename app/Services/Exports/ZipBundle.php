<?php

declare(strict_types=1);

namespace App\Services\Exports;

use RuntimeException;
use ZipArchive;

/**
 * Writes a ZIP from files already on disk, failing loudly instead of leaving a
 * half-built archive for the export job to upload.
 *
 * ZipArchive reads every added file when the archive is CLOSED, not when it is
 * added, and reports problems as `false` rather than exceptions. The exporters
 * that bundle (csa_bundle, las_bundle) used to ignore those returns, so a
 * failed close left a missing or empty file for the job to measure and ship.
 */
final class ZipBundle
{
    /**
     * @param array<string, string> $entries entry name inside the archive => path of the file to put there
     *
     * @throws RuntimeException when the archive cannot be created, filled or written
     */
    public static function write(string $zipPath, array $entries): void
    {
        $zip = new ZipArchive;
        if ($zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
            throw new RuntimeException("Cannot create ZIP archive at: {$zipPath}");
        }

        try {
            foreach ($entries as $name => $path) {
                // is_file() first: addFile() answers a missing file with a PHP
                // warning AND false, and a warning is not an error we can name.
                if (! is_file($path) || ! $zip->addFile($path, (string) $name)) {
                    throw new RuntimeException("Cannot add {$name} to the ZIP archive.");
                }
            }

            if (! $zip->close()) {
                throw new RuntimeException("Cannot write ZIP archive at: {$zipPath}");
            }
        } catch (\Throwable $e) {
            // Forget the queued files. The caller deletes them next, and the
            // archive's destructor would otherwise go to read them, fail, and
            // let that warning replace the error that actually stopped the export.
            try {
                $zip->unchangeAll();
            } catch (\Throwable) {
                // Already closed, or never usable; nothing is queued.
            }

            throw $e;
        }
    }
}
