<?php

declare(strict_types=1);

namespace Tests\Unit\Support;

use App\Support\UploadContentGuard;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;
use ZipArchive;

/**
 * The upload edge used to trust a client-supplied filename and nothing else.
 *
 * Neither controller had a `mimes:` rule; both decided acceptance AND routing
 * from `getClientOriginalExtension()`. A 40 KB zip bomb renamed `report.pdf`
 * was stored under `reports/` and dispatched to `ingest_pdf`.
 *
 * MOST OF THIS FILE IS ABOUT NOT REJECTING THINGS
 *     The two failure directions are not symmetric. A false rejection costs a
 *     geologist their data and produces a support ticket; a false acceptance
 *     costs a worker a few minutes failing on a bad file. So the guard is
 *     built to stay silent whenever it is unsure, and the tests that matter
 *     most are the ones proving it stays silent on the formats this platform
 *     actually receives — a LAS log and a CSV both sniff as `text/plain`, a
 *     GeoPackage is a SQLite file, an XLSX is a ZIP, and a shapefile is
 *     octet-stream along with half of geoscience.
 */
final class UploadContentGuardTest extends TestCase
{
    /** @var list<string> */
    private array $tempFiles = [];

    protected function tearDown(): void
    {
        foreach ($this->tempFiles as $path) {
            if (is_file($path)) {
                @unlink($path);
            }
        }

        parent::tearDown();
    }

    // ── MIME: what it catches ───────────────────────────────────────────

    #[Test]
    public function a_zip_wearing_a_pdf_extension_is_rejected(): void
    {
        // The case that motivated the guard.
        self::assertTrue(
            UploadContentGuard::mimeMismatch('pdf', 'application/zip'),
        );
    }

    #[Test]
    public function an_image_named_as_a_report_is_rejected(): void
    {
        self::assertTrue(UploadContentGuard::mimeMismatch('pdf', 'image/png'));
    }

    #[Test]
    public function matching_is_case_insensitive_on_both_sides(): void
    {
        self::assertFalse(UploadContentGuard::mimeMismatch('PDF', 'APPLICATION/PDF'));
        self::assertTrue(UploadContentGuard::mimeMismatch('PDF', 'IMAGE/PNG'));
    }

    // ── MIME: what it deliberately lets through ─────────────────────────

    /** @return array<string, array{0: string, 1: ?string}> */
    public static function ambiguousCases(): array
    {
        return [
            // No reliable signature — every one of these is a real format
            // this platform accepts.
            'LAS well log sniffs as text' => ['las', 'text/plain'],
            'collar CSV sniffs as text' => ['csv', 'text/plain'],
            'CSV sniffed as csv' => ['csv', 'text/csv'],
            'shapefile is octet-stream' => ['shp', 'application/octet-stream'],
            'FlatGeobuf is octet-stream' => ['fgb', 'application/octet-stream'],
            'GeoJSON sniffs as json or text' => ['geojson', 'application/json'],
            'DXF sniffs as text' => ['dxf', 'text/plain'],
            'QGIS project is xml' => ['qgs', 'text/xml'],

            // "No opinion" results, whatever the extension.
            'empty sniff' => ['pdf', ''],
            'null sniff' => ['pdf', null],
            'octet-stream against a known ext' => ['pdf', 'application/octet-stream'],
            'empty file' => ['pdf', 'application/x-empty'],
        ];
    }

    #[Test]
    #[DataProvider('ambiguousCases')]
    public function an_ambiguous_case_is_never_a_mismatch(
        string $extension,
        ?string $sniffed,
    ): void {
        self::assertFalse(
            UploadContentGuard::mimeMismatch($extension, $sniffed),
            "rejecting {$extension}/{$sniffed} would cost a real upload",
        );
    }

    #[Test]
    public function an_xlsx_sniffed_as_a_zip_is_accepted(): void
    {
        // An OOXML workbook IS a zip container. finfo reports either the
        // specific OOXML type or the generic one depending on its magic
        // database version, and both are correct — so a strict map would
        // reject half the Excel uploads on some hosts and not others.
        self::assertFalse(UploadContentGuard::mimeMismatch('xlsx', 'application/zip'));
        self::assertFalse(UploadContentGuard::mimeMismatch(
            'xlsx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        ));
    }

    #[Test]
    public function an_unknown_extension_is_never_rejected(): void
    {
        // The map covers only formats with a reliable signature. An
        // extension absent from it must fall through, not fail closed —
        // the category check upstream already decides what is accepted.
        self::assertFalse(UploadContentGuard::mimeMismatch('kmz', 'application/zip'));
        self::assertFalse(UploadContentGuard::mimeMismatch('segy', 'audio/x-wav'));
    }

    // ── Archives ────────────────────────────────────────────────────────

    private function makeZip(callable $build): string
    {
        $path = tempnam(sys_get_temp_dir(), 'guard').'.zip';
        $this->tempFiles[] = $path;

        $zip = new ZipArchive;
        self::assertTrue($zip->open($path, ZipArchive::CREATE | ZipArchive::OVERWRITE));
        $build($zip);
        $zip->close();

        return $path;
    }

    #[Test]
    public function an_ordinary_archive_is_accepted(): void
    {
        $path = $this->makeZip(function (ZipArchive $zip): void {
            $zip->addFromString('collars.csv', "hole_id,east,north\nDH1,1,2\n");
            $zip->addFromString('survey/deviation.csv', "hole_id,depth\nDH1,10\n");
        });

        self::assertNull(UploadContentGuard::rejectArchive($path));
    }

    #[Test]
    public function a_file_that_is_not_a_zip_is_rejected(): void
    {
        $path = tempnam(sys_get_temp_dir(), 'guard');
        $this->tempFiles[] = $path;
        file_put_contents($path, "%PDF-1.4\nnot a zip at all\n");

        $reason = UploadContentGuard::rejectArchive($path);

        self::assertNotNull($reason);
        self::assertStringContainsString('not a readable ZIP', $reason);
    }

    #[Test]
    public function a_traversal_entry_is_rejected(): void
    {
        // Both extractors guard this, but an archive containing one is
        // malformed or hostile and there is no reason to store it first.
        $path = $this->makeZip(function (ZipArchive $zip): void {
            $zip->addFromString('ok.csv', 'a,b');
            $zip->addFromString('../escaped.csv', 'a,b');
        });

        $reason = UploadContentGuard::rejectArchive($path);

        self::assertNotNull($reason);
        self::assertStringContainsString('outside the extraction directory', $reason);
    }

    #[Test]
    public function a_nested_traversal_entry_is_rejected(): void
    {
        $path = $this->makeZip(function (ZipArchive $zip): void {
            $zip->addFromString('data/../../escaped.csv', 'a,b');
        });

        self::assertNotNull(UploadContentGuard::rejectArchive($path));
    }

    #[Test]
    public function a_filename_merely_containing_dots_is_not_traversal(): void
    {
        // `..` has to be a whole path segment. Rejecting "v..2/data.csv" or
        // "survey..final.csv" would be a false positive on ordinary,
        // if untidy, real-world naming.
        $path = $this->makeZip(function (ZipArchive $zip): void {
            $zip->addFromString('survey..final.csv', 'a,b');
            $zip->addFromString('v..2/data.csv', 'a,b');
        });

        self::assertNull(UploadContentGuard::rejectArchive($path));
    }

    #[Test]
    public function an_absolute_entry_path_is_rejected(): void
    {
        $path = $this->makeZip(function (ZipArchive $zip): void {
            $zip->addFromString('/etc/passwd', 'root:x:0:0');
        });

        self::assertNotNull(UploadContentGuard::rejectArchive($path));
    }

    #[Test]
    public function the_expansion_cap_is_read_from_the_central_directory(): void
    {
        // The cost of this check must not scale with the decompressed
        // size — that is the whole reason it can run at the edge. A zip
        // bomb is small on disk and enormous expanded; `statIndex` reads
        // the declared size without inflating anything.
        $path = $this->makeZip(function (ZipArchive $zip): void {
            $zip->addFromString('bomb.txt', str_repeat('0', 1024));
        });

        $before = microtime(true);
        UploadContentGuard::rejectArchive($path);
        $elapsed = microtime(true) - $before;

        self::assertLessThan(1.0, $elapsed);
    }

    // ── Cross-language cap parity ───────────────────────────────────────

    #[Test]
    public function the_caps_match_the_workers_own_caps(): void
    {
        // These constants are duplicated in Python because the two edges
        // are in different languages. Duplication is only safe while
        // something fails when they drift — this is that something.
        //
        // If the worker's caps are relaxed and these are not, the edge
        // rejects archives the worker would have accepted, which is a
        // silent capability regression: the user is told their delivery
        // is too large by a service that would have handled it.
        $python = file_get_contents(
            dirname(__DIR__, 3)
            .'/src/fastapi/app/hatchet_workflows/ingest_spatial.py',
        );
        self::assertNotFalse($python);

        self::assertMatchesRegularExpression(
            '/_MAX_ARCHIVE_ENTRIES\s*=\s*50_000/',
            (string) $python,
            'ingest_spatial._MAX_ARCHIVE_ENTRIES no longer matches '
            .'UploadContentGuard::MAX_ARCHIVE_ENTRIES ('
            .UploadContentGuard::MAX_ARCHIVE_ENTRIES.')',
        );
        self::assertSame(50_000, UploadContentGuard::MAX_ARCHIVE_ENTRIES);

        self::assertMatchesRegularExpression(
            '/_MAX_EXPANDED_BYTES\s*=\s*2 \* 1024 \* 1024 \* 1024/',
            (string) $python,
            'ingest_spatial._MAX_EXPANDED_BYTES no longer matches '
            .'UploadContentGuard::MAX_EXPANDED_BYTES',
        );
        self::assertSame(2 * 1024 * 1024 * 1024, UploadContentGuard::MAX_EXPANDED_BYTES);
    }
}
