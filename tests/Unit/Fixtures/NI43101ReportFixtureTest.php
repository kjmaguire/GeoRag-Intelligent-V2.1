<?php

namespace Tests\Unit\Fixtures;

use Tests\TestCase;

class NI43101ReportFixtureTest extends TestCase
{
    /**
     * Path to the generated NI 43-101 technical report fixture.
     */
    protected const FIXTURE_PATH = __DIR__.'/../../fixtures/reports/PLS-2024-Technical-Report.pdf';

    /**
     * The fixture under tests/fixtures/reports/ is a generated artifact and is
     * deliberately gitignored (.gitignore: tests/fixtures/reports/*.pdf), so it
     * is absent on fresh clones and in CI until generated. Skip the whole class
     * when it is missing — mirroring the Python consumers
     * (tests/fixtures/ocr → pytest.skip / skipif) and the skipIfSqlite house
     * style — rather than reporting a hard failure for an optional fixture.
     */
    protected function setUp(): void
    {
        parent::setUp();

        if (! file_exists(self::FIXTURE_PATH)) {
            $this->markTestSkipped(
                'NI 43-101 fixture not generated. Create it with: '
                .'python tests/fixtures/reports/generate_test_report.py',
            );
        }
    }

    /**
     * Check that the NI 43-101 fixture file exists.
     */
    public function test_ni43101_fixture_file_exists(): void
    {
        $this->assertFileExists(self::FIXTURE_PATH);
    }

    /**
     * Verify that the fixture is a valid PDF file.
     */
    public function test_ni43101_fixture_is_valid_pdf(): void
    {
        $this->assertFileExists(self::FIXTURE_PATH);

        $content = file_get_contents(self::FIXTURE_PATH);

        // Check for PDF file signature
        $this->assertStringStartsWith(
            '%PDF',
            $content,
            'Fixture file does not have valid PDF signature',
        );

        // Check for EOF marker
        $this->assertStringEndsWith(
            '%%EOF',
            trim($content),
            'Fixture file does not have valid PDF EOF marker',
        );
    }

    /**
     * Verify that the fixture's compressed text streams are present by
     * checking the PDF object/trailer structure. Raw content-string
     * matching against the bytes does NOT work because ReportLab emits
     * FlateDecoded streams; "Patterson Lake South" is in the text layer
     * but compressed in the file. Content-level verification belongs in
     * a Python fixture test that uses PyPDF2 (see
     * tests/fixtures/reports/verify_and_build.py) or in an ingestion
     * integration test that parses the PDF through the ingestion
     * pipeline's extractor.
     *
     * Here we just verify the PDF has a catalog and at least one
     * FlateDecode-compressed content stream — a lightweight smoke test
     * that the generator produced a real multi-page document rather
     * than an empty / corrupt file.
     */
    public function test_ni43101_fixture_contains_compressed_content_streams(): void
    {
        $this->assertFileExists(self::FIXTURE_PATH);

        $content = file_get_contents(self::FIXTURE_PATH);

        $this->assertStringContainsString(
            '/Catalog',
            $content,
            'Fixture is missing PDF /Catalog object — generator output is malformed',
        );

        $this->assertStringContainsString(
            'FlateDecode',
            $content,
            'Fixture has no FlateDecode streams — generator appears to have produced an empty PDF',
        );
    }

    /**
     * Verify that the fixture file has reasonable size for a 7-page
     * compressed NI 43-101 technical report. Actual output from the
     * generator (as of April 2026) is ~17 KB. If the generator starts
     * embedding images, figures, or uncompressed tables the upper bound
     * here will need to loosen; if it shrinks below 5 KB it's almost
     * certainly a regression in the generator itself.
     */
    public function test_ni43101_fixture_has_reasonable_size(): void
    {
        $this->assertFileExists(self::FIXTURE_PATH);

        $fileSize = filesize(self::FIXTURE_PATH);

        $this->assertGreaterThan(
            5 * 1024,
            $fileSize,
            'Fixture file is unexpectedly small — did the generator produce only a title page?',
        );

        $this->assertLessThan(
            500 * 1024,
            $fileSize,
            'Fixture file is unexpectedly large — check the generator is not embedding unintended assets',
        );
    }
}
