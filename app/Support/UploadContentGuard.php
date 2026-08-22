<?php

declare(strict_types=1);

namespace App\Support;

use ZipArchive;

/**
 * Content checks at the upload edge, before anything is stored or dispatched.
 *
 * Both upload controllers decided acceptance and routing purely from
 * `getClientOriginalExtension()` — a string the client controls. A 40 KB zip
 * bomb renamed to `report.pdf` passed validation, landed in bronze, and was
 * dispatched to `ingest_pdf`, which spent worker time and memory failing on a
 * file that was never a PDF. `DrillUploadController` already sniffed the real
 * MIME type with finfo, but only to persist it.
 *
 * WHY THE MIME CHECK IS DELIBERATELY LENIENT
 *     A false rejection costs a geologist their data and a support ticket; a
 *     false acceptance costs a worker a few minutes failing on a bad file.
 *     Those are not symmetric, so this rejects only on a CLEAR contradiction
 *     and lets anything ambiguous through.
 *
 *     Ambiguity is the normal case here, not the exception. finfo reads magic
 *     bytes, and most of the formats this platform accepts have none worth
 *     the name: a LAS well log, a CSV and a QGIS project all sniff as
 *     `text/plain`; a `.gpkg` is a SQLite database; a `.xlsx` is a ZIP; a
 *     `.shp` and a `.fgb` are `application/octet-stream` along with half the
 *     binary formats in geoscience. Anything that resolves to octet-stream or
 *     an empty result is therefore treated as "no opinion", not as a
 *     mismatch.
 *
 *     What this DOES catch is the case worth catching: a file whose magic
 *     bytes say one specific thing and whose extension claims a different
 *     specific thing. ZIP-magic bytes under a `.pdf` name is the example that
 *     motivated it.
 */
final class UploadContentGuard
{
    /**
     * Extension → MIME types finfo may legitimately report for it.
     *
     * Only extensions with a RELIABLE signature appear here. An extension
     * absent from this map is never rejected — see `mimeMismatch()`.
     *
     * @var array<string, list<string>>
     */
    private const EXPECTED_MIMES = [
        'pdf' => ['application/pdf'],
        'tif' => ['image/tiff'],
        'tiff' => ['image/tiff'],
        'zip' => ['application/zip', 'application/x-zip-compressed'],
        // OOXML workbooks ARE zip containers, and finfo reports either the
        // specific type or the generic one depending on the magic database
        // version. Both are correct.
        'xlsx' => [
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/zip',
            'application/x-zip-compressed',
        ],
        'xlsm' => [
            'application/vnd.ms-excel.sheet.macroEnabled.12',
            'application/zip',
            'application/x-zip-compressed',
        ],
        'xls' => ['application/vnd.ms-excel', 'application/x-ole-storage'],
        'gpkg' => ['application/x-sqlite3', 'application/vnd.sqlite3'],
        'qgz' => ['application/zip', 'application/x-zip-compressed'],
    ];

    /** finfo results that mean "no opinion" rather than a contradiction. */
    private const NO_OPINION = [
        '',
        'application/octet-stream',
        'application/x-empty',
        'inode/x-empty',
    ];

    /**
     * Cap on entries in an uploaded archive.
     *
     * Mirrors `_MAX_ARCHIVE_ENTRIES` in
     * src/fastapi/app/hatchet_workflows/ingest_spatial.py. Duplicated rather
     * than derived because the two edges are in different languages;
     * `UploadContentGuardTest` reads the Python constant and fails if they
     * drift, which is the only thing that makes duplication safe.
     */
    public const MAX_ARCHIVE_ENTRIES = 50_000;

    /**
     * Cap on the total uncompressed size of an uploaded archive.
     *
     * Mirrors `_MAX_EXPANDED_BYTES` in ingest_spatial.py — 2 GiB. Checking it
     * here as well as there is the point: the worker's check happens after
     * the object has been stored and a workflow dispatched, so a bomb still
     * costs storage and a run. ZipArchive reads only the central directory,
     * so this is cheap.
     */
    public const MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024;

    /**
     * True when the sniffed MIME type contradicts the declared extension.
     *
     * False for every ambiguous case: unknown extension, no-opinion sniff,
     * or an extension with no reliable signature.
     */
    public static function mimeMismatch(string $extension, ?string $sniffed): bool
    {
        $ext = strtolower($extension);
        $mime = strtolower(trim((string) $sniffed));

        if (! array_key_exists($ext, self::EXPECTED_MIMES)) {
            return false;
        }
        if (in_array($mime, self::NO_OPINION, true)) {
            return false;
        }

        return ! in_array($mime, self::EXPECTED_MIMES[$ext], true);
    }

    /**
     * Inspect an uploaded ZIP's central directory.
     *
     * Returns null when the archive is acceptable, or a human-readable
     * reason when it is not. Reads the directory only — no extraction — so
     * the cost is independent of the decompressed size, which is the whole
     * reason this can run at the edge.
     *
     * Three refusals, in the order they matter:
     *   - unreadable: not a ZIP at all, or truncated;
     *   - traversal: an entry whose path escapes the extraction root. The
     *     extractors both guard this too, but an archive containing one is
     *     malformed or hostile and there is no reason to store it;
     *   - size/count: the zip-bomb shape.
     */
    public static function rejectArchive(string $path): ?string
    {
        $zip = new ZipArchive;
        $opened = $zip->open($path, ZipArchive::RDONLY);

        if ($opened !== true) {
            return 'The file is not a readable ZIP archive (code '.$opened.').';
        }

        try {
            if ($zip->numFiles > self::MAX_ARCHIVE_ENTRIES) {
                return sprintf(
                    'The archive holds %d entries, over the %d cap. Split the '
                    .'delivery into smaller archives.',
                    $zip->numFiles,
                    self::MAX_ARCHIVE_ENTRIES,
                );
            }

            $expanded = 0;

            for ($i = 0; $i < $zip->numFiles; $i++) {
                $stat = $zip->statIndex($i);
                if ($stat === false) {
                    return 'The archive central directory is unreadable at entry '.$i.'.';
                }

                $name = (string) $stat['name'];
                if (
                    str_starts_with($name, '/')
                    || str_starts_with($name, '\\')
                    || preg_match('#(^|[/\\\\])\.\.([/\\\\]|$)#', $name) === 1
                ) {
                    return 'The archive contains an entry that would be written '
                        .'outside the extraction directory ('.$name.'). Ask for '
                        .'a fresh copy.';
                }

                $expanded += (int) $stat['size'];
                if ($expanded > self::MAX_EXPANDED_BYTES) {
                    return sprintf(
                        'The archive expands to more than %d bytes, over the cap. '
                        .'Split the delivery into smaller archives.',
                        self::MAX_EXPANDED_BYTES,
                    );
                }
            }
        } finally {
            $zip->close();
        }

        return null;
    }
}
