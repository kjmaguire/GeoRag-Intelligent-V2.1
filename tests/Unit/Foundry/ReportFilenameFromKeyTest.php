<?php

declare(strict_types=1);

namespace Tests\Unit\Foundry;

use App\Http\Controllers\Foundry\ReportController;
use PHPUnit\Framework\TestCase;

/**
 * The Reports list shows the uploaded file's own name. `silver.reports.title`
 * is parsed out of the document and has arrived as `<figure>` and as single
 * letters, so the name has to be recovered from the storage key instead.
 *
 * Three writers mint those keys and they do not agree on shape. This pins all
 * three, because a pattern that handles only two leaves a machine string in
 * front of the filename — which looks exactly like the bug it was meant to fix.
 */
final class ReportFilenameFromKeyTest extends TestCase
{
    public function test_strips_the_upload_controller_prefix(): void
    {
        // {Ymd_His}_{name}
        $this->assertSame(
            'FA16099231_edit.csv',
            ReportController::filenameFromKey('reports/abc/20260824_204518_FA16099231_edit.csv'),
        );
    }

    public function test_strips_the_drill_upload_sha_prefix(): void
    {
        // {Ymd_His}_{sha8}_{name}
        $this->assertSame(
            'collars.csv',
            ReportController::filenameFromKey('drill/p/20260824_204518_a1b2c3d4_collars.csv'),
        );
    }

    public function test_strips_the_zip_fan_out_microsecond_prefix(): void
    {
        // strftime('%Y%m%d_%H%M%S_%f') — six DECIMAL digits, not eight hex.
        // This is the shape that was left glued to every file extracted from
        // an archive.
        $this->assertSame(
            'geology_poly.shp',
            ReportController::filenameFromKey('spatial/p/20260824_204518_123456_geology_poly.shp'),
        );
    }

    public function test_keeps_a_name_the_geologist_chose_that_looks_like_a_prefix(): void
    {
        // Their own date-stamped name has no seconds field, so it survives.
        $this->assertSame(
            '20260824_survey.csv',
            ReportController::filenameFromKey('reports/p/20260824_survey.csv'),
        );
    }

    public function test_strips_the_tiff_derived_prefix_and_then_the_upload_prefix(): void
    {
        // A TIFF/RRD is normalised to a PDF under its own key, and THAT is
        // what silver.reports points at. Both layers of machine string have to
        // come off or the Reports list shows the derivation bookkeeping as the
        // document's name.
        $this->assertSame(
            'Geologic_Map_Unga_1982_color_utm.pdf',
            ReportController::filenameFromKey(
                'reports/p/tiff-derived-a1b2c3d4-20260824_204518_Geologic_Map_Unga_1982_color_utm.pdf',
            ),
        );
    }

    public function test_a_real_name_beginning_with_tiff_is_not_mistaken_for_a_derivation(): void
    {
        // The pattern requires the literal 'tiff-derived-' plus exactly eight
        // hex characters, so a genuine file called 'tiff-derived-notes.pdf'
        // keeps its name.
        $this->assertSame(
            'tiff-derived-notes.pdf',
            ReportController::filenameFromKey('reports/p/20260824_204518_tiff-derived-notes.pdf'),
        );
    }

    public function test_handles_a_key_with_no_directory(): void
    {
        $this->assertSame('a.pdf', ReportController::filenameFromKey('20260824_204518_a.pdf'));
    }

    public function test_never_returns_an_empty_string(): void
    {
        // A key that is nothing but a prefix keeps the segment — at least the
        // operator can search for it.
        $this->assertSame(
            '20260824_204518_',
            ReportController::filenameFromKey('reports/p/20260824_204518_'),
        );
    }

    public function test_a_missing_key_is_null_not_a_guess(): void
    {
        $this->assertNull(ReportController::filenameFromKey(null));
        $this->assertNull(ReportController::filenameFromKey(''));
    }

    public function test_preserves_spaces_and_punctuation_in_real_names(): void
    {
        $this->assertSame(
            'C 5 - Diamond Drill Holes 21 - 43 Trenches 1-44.pdf',
            ReportController::filenameFromKey(
                'reports/p/20260824_204518_C 5 - Diamond Drill Holes 21 - 43 Trenches 1-44.pdf',
            ),
        );
    }
}
