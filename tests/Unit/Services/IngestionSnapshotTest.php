<?php

declare(strict_types=1);

namespace Tests\Unit\Services;

use App\Services\IngestionSnapshot;
use Carbon\CarbonImmutable;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * IngestionSnapshot::assemble() — which uploaded files are still ingesting.
 *
 * The pure half of the snapshot, so it runs in the default SQLite suite with
 * no database and no object store. The Postgres-backed feature tests
 * (IngestionRunsControllerTest, OverviewControllerTest) cover the loaders.
 *
 * The bug this pins (2026-09-29, "waffles" project): the Overview tile read
 * "10 files ingesting" for NI 43-101s that had finished, because an upload
 * was matched to its report by whether the filename STARTED WITH the
 * report's title — and a report's title is the document's own extracted
 * title, not the upload filename. silver.reports.source_object_key is the
 * exact bronze key the report was parsed from.
 */
final class IngestionSnapshotTest extends TestCase
{
    private const PROJECT = '11111111-2222-3333-4444-555555555555';

    private const MADSEN = 'reports/'.self::PROJECT.'/20260929_012744_SRKCA_MadsenPFS_NI43-101_CAPR003299_Final_20250218.pdf';

    private function snapshot(): IngestionSnapshot
    {
        return app(IngestionSnapshot::class);
    }

    /**
     * @return array{report_id: string, title: string, source_object_key: ?string, parser_used: ?string, parse_quality_pct: ?float, text_page_coverage_pct: ?float, is_scanned: bool, passages: int, embedded: int}
     */
    private function report(string $id, string $title, ?string $sourceKey): array
    {
        return [
            'report_id' => $id,
            'title' => $title,
            'source_object_key' => $sourceKey,
            'parser_used' => 'fitz',
            'parse_quality_pct' => 50.0,
            'text_page_coverage_pct' => 100.0,
            'is_scanned' => false,
            'passages' => 10,
            'embedded' => 10,
        ];
    }

    /**
     * @return array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}
     */
    private function upload(string $key, ?string $uploadedAt = null): array
    {
        return [
            'key' => $key,
            'filename' => basename($key),
            'size_bytes' => 1024,
            'uploaded_at' => $uploadedAt ?? CarbonImmutable::now()->subHour()->toIso8601String(),
        ];
    }

    /**
     * @param array<string, mixed> $overrides
     *
     * @return array{minio_key: string, filename: string, current_step: string, step_index: int, total_steps: int, stage_pct: ?float, stage_detail: ?string, started_at: ?string, updated_at: ?string, failed_at: ?string, error_text: ?string, report_id: ?string, status: string, rows_written: ?int, warnings: list<array<string, mixed>>}
     */
    private function progress(string $key, array $overrides = []): array
    {
        $now = CarbonImmutable::now()->toIso8601String();

        /** @var array{minio_key: string, filename: string, current_step: string, step_index: int, total_steps: int, stage_pct: ?float, stage_detail: ?string, started_at: ?string, updated_at: ?string, failed_at: ?string, error_text: ?string, report_id: ?string, status: string, rows_written: ?int, warnings: list<array<string, mixed>>} */
        return array_merge([
            'minio_key' => $key,
            'filename' => basename($key),
            'current_step' => 'parse',
            'step_index' => 2,
            'total_steps' => 5,
            'stage_pct' => null,
            'stage_detail' => null,
            'started_at' => $now,
            'updated_at' => $now,
            'failed_at' => null,
            'error_text' => null,
            'report_id' => null,
            'status' => 'started',
            'rows_written' => null,
            'warnings' => [],
        ], $overrides);
    }

    public function test_an_upload_matched_by_source_object_key_is_not_in_flight_whatever_its_title(): void
    {
        // The reported case, verbatim: the title shares no prefix with the
        // filename, which is normal for a real NI 43-101.
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Madsen Mine Pre-Feasibility Study Technical Report', self::MADSEN)],
            [],
            [$this->upload(self::MADSEN, '2026-09-29T01:27:44+00:00')],
        );

        $this->assertSame(0, $runs['totals']['in_flight']);
        $this->assertSame([], $runs['in_flight']);
        $this->assertNull($runs['latest_in_flight']);
        $this->assertSame(1, $runs['totals']['completed']);
        // The completed card can now name the file it came from.
        $this->assertSame(basename(self::MADSEN), $runs['completed'][0]['filename']);
        $this->assertSame('2026-09-29T01:27:44+00:00', $runs['completed'][0]['uploaded_at']);
    }

    public function test_an_upload_with_no_report_and_no_progress_row_is_in_flight(): void
    {
        $runs = $this->snapshot()->assemble([], [], [$this->upload(self::MADSEN)]);

        $this->assertSame(1, $runs['totals']['in_flight']);
        $this->assertSame('queued', $runs['in_flight'][0]['status']);
        $this->assertSame(basename(self::MADSEN), $runs['latest_in_flight']);
    }

    public function test_an_upload_with_a_completed_progress_row_is_not_in_flight(): void
    {
        $runs = $this->snapshot()->assemble(
            [],
            [$this->progress(self::MADSEN, ['current_step' => 'completed', 'step_index' => 5, 'status' => 'completed'])],
            [$this->upload(self::MADSEN)],
        );

        $this->assertSame(0, $runs['totals']['in_flight']);
        $this->assertSame(1, $runs['totals']['files_completed']);
    }

    public function test_a_completed_pdf_run_with_a_null_report_id_is_not_listed_twice(): void
    {
        // mark_report_id() only stamps a row that is still non-terminal, so
        // a completed PDF run routinely carries report_id NULL. The report
        // is still found by key, so the run is not also rendered as a green
        // row beside the completed card that already shows it.
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Some Extracted Title', self::MADSEN)],
            [$this->progress(self::MADSEN, ['current_step' => 'completed', 'step_index' => 5, 'status' => 'completed'])],
            [$this->upload(self::MADSEN)],
        );

        $this->assertSame([], $runs['in_flight']);
        $this->assertSame(1, $runs['totals']['completed']);
        $this->assertSame(basename(self::MADSEN), $runs['completed'][0]['filename']);
    }

    public function test_a_quiet_non_terminal_run_whose_report_exists_counts_as_completed(): void
    {
        // A worker killed mid-embed (Spot interruption, the 17:00 nightly
        // stop) leaves the row at 'embedding' with status 'started'. The
        // report exists, and nothing has touched the row for an hour: the
        // document ingested.
        $anHourAgo = CarbonImmutable::now()->subHour()->toIso8601String();
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Some Extracted Title', self::MADSEN)],
            [$this->progress(self::MADSEN, [
                'current_step' => 'embedding', 'step_index' => 4,
                'started_at' => $anHourAgo, 'updated_at' => $anHourAgo,
            ])],
            [$this->upload(self::MADSEN)],
        );

        $this->assertSame(0, $runs['totals']['in_flight']);
        $this->assertSame([], $runs['in_flight']);
        $this->assertSame(1, $runs['totals']['files_completed']);
        $this->assertSame(0, $runs['totals']['files_running']);
    }

    public function test_a_live_non_terminal_run_whose_report_exists_is_still_moving(): void
    {
        // The report row is written at persist, BEFORE the embed steps. A
        // run that is visibly still working (updated a moment ago) must not
        // be called idle just because its report already exists.
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Some Extracted Title', self::MADSEN)],
            [$this->progress(self::MADSEN, ['current_step' => 'embedding', 'step_index' => 4])],
            [$this->upload(self::MADSEN)],
        );

        $this->assertSame(1, $runs['totals']['in_flight']);
        $this->assertSame(1, $runs['totals']['files_running']);
    }

    public function test_a_quiet_non_terminal_run_with_no_report_is_still_in_flight(): void
    {
        // Nothing proves this one ingested. It stays "in flight" until
        // stale_run_detector closes it as timed_out — the display does not
        // invent a verdict the pipeline has not reached.
        $anHourAgo = CarbonImmutable::now()->subHour()->toIso8601String();
        $runs = $this->snapshot()->assemble(
            [],
            [$this->progress(self::MADSEN, ['started_at' => $anHourAgo, 'updated_at' => $anHourAgo])],
            [$this->upload(self::MADSEN)],
        );

        $this->assertSame(1, $runs['totals']['in_flight']);
    }

    public function test_a_tiff_upload_matches_the_report_of_its_derived_pdf(): void
    {
        // tiff_normalize writes the PDF it derives under reports/ as
        // tiff-derived-{sha8}-{stem}.pdf, and THAT key is the report's
        // source_object_key. The TIFF itself never has a report of its own.
        $tiff = 'tiff/'.self::PROJECT.'/20260901_101010_Old_Scan_1987.tif';
        $derived = 'reports/'.self::PROJECT.'/tiff-derived-0a1b2c3d-20260901_101010_Old_Scan_1987.pdf';

        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Assessment Report 1987', $derived)],
            [],
            [$this->upload($tiff), $this->upload($derived)],
        );

        $this->assertSame(0, $runs['totals']['in_flight']);
        $this->assertSame([], $runs['in_flight']);
    }

    public function test_the_title_fallback_applies_only_to_reports_with_no_source_key(): void
    {
        // Legacy report (no key): the old title-prefix match still claims it.
        $legacy = 'reports/'.self::PROJECT.'/20260524_120000_Madsen_NI_43-101_Final.pdf';
        $runs = $this->snapshot()->assemble(
            [$this->report('r-legacy', 'Madsen NI 43-101 Final', null)],
            [],
            [$this->upload($legacy)],
        );
        $this->assertSame(0, $runs['totals']['in_flight']);
        $this->assertSame(basename($legacy), $runs['completed'][0]['filename']);

        // A report that KNOWS its key is never matched by title: a second
        // upload whose name merely starts with that title is its own file,
        // and it has not ingested.
        $other = 'reports/'.self::PROJECT.'/20260929_090000_Madsen_NI_43-101_Final_v2.pdf';
        $runs = $this->snapshot()->assemble(
            [$this->report('r-keyed', 'Madsen NI 43-101 Final', $legacy)],
            [],
            [$this->upload($legacy), $this->upload($other)],
        );
        $this->assertSame(1, $runs['totals']['in_flight']);
        $this->assertSame(basename($other), $runs['latest_in_flight']);
    }

    public function test_an_all_digit_title_fingerprint_does_not_type_error(): void
    {
        // PHP turns an array key of "12345" into int 12345; the old code
        // needed a defensive cast (2026-05-25, cameco-shirley-basin).
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', '12345', null)],
            [],
            [$this->upload('reports/'.self::PROJECT.'/20260524_120000_12345_scan.pdf')],
        );

        $this->assertSame(0, $runs['totals']['in_flight']);
    }

    public function test_latest_in_flight_skips_settled_rows(): void
    {
        // A failed run in its 24 h window sorts first (newest), but it is not
        // moving — "latest: <a failed file>" beside "1 file ingesting" would
        // name the wrong file.
        $now = CarbonImmutable::now();
        $runs = $this->snapshot()->assemble(
            [],
            [
                $this->progress('reports/'.self::PROJECT.'/20260929_020000_failed.pdf', [
                    'current_step' => 'failed', 'status' => 'failed',
                    'failed_at' => $now->toIso8601String(),
                    'started_at' => $now->toIso8601String(),
                ]),
                $this->progress('reports/'.self::PROJECT.'/20260929_010000_moving.pdf', [
                    'started_at' => $now->subMinute()->toIso8601String(),
                ]),
            ],
            [],
        );

        $this->assertSame(1, $runs['totals']['in_flight']);
        // The settled row IS first in the list...
        $this->assertSame('20260929_020000_failed.pdf', $runs['in_flight'][0]['filename']);
        // ...and is still not what the tile names.
        $this->assertSame('20260929_010000_moving.pdf', $runs['latest_in_flight']);
    }

    public function test_the_file_ledger_still_partitions_files(): void
    {
        $anHourAgo = CarbonImmutable::now()->subHour()->toIso8601String();
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Stuck but ingested', self::MADSEN)],
            [
                $this->progress(self::MADSEN, ['current_step' => 'embedding', 'updated_at' => $anHourAgo, 'started_at' => $anHourAgo]),
                $this->progress('collars/'.self::PROJECT.'/a.csv', ['current_step' => 'completed', 'status' => 'completed']),
                $this->progress('collars/'.self::PROJECT.'/b.csv', ['current_step' => 'completed', 'status' => 'partial']),
                $this->progress('collars/'.self::PROJECT.'/c.csv', ['current_step' => 'failed', 'status' => 'failed']),
                $this->progress('collars/'.self::PROJECT.'/d.csv'),
            ],
            [],
        );

        $t = $runs['totals'];
        $this->assertSame(5, $t['files']);
        $this->assertSame(
            $t['files'],
            $t['files_completed'] + $t['files_partial'] + $t['files_failed'] + $t['files_timed_out'] + $t['files_running'],
        );
        $this->assertSame(2, $t['files_completed']);
        $this->assertSame(1, $t['files_running']);
    }

    public function test_the_overview_summary_is_a_projection_of_the_snapshot(): void
    {
        // What the Overview tile renders server-side must be exactly what
        // the frontend's poll of ingestion-runs.json derives a few seconds
        // later: totals.in_flight, totals.completed, latest_in_flight.
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Unrelated Title', self::MADSEN)],
            [$this->progress('collars/'.self::PROJECT.'/20260929_030000_moving.csv')],
            [$this->upload(self::MADSEN), $this->upload('reports/'.self::PROJECT.'/20260929_040000_new.pdf')],
        );

        $this->assertSame([
            'in_flight' => $runs['totals']['in_flight'],
            'completed' => $runs['totals']['completed'],
            'latest_in_flight' => $runs['latest_in_flight'],
        ], IngestionSnapshot::summarise($runs));
        $this->assertSame(2, $runs['totals']['in_flight']);
        $this->assertSame(1, $runs['totals']['completed']);
    }

    /**
     * @return array<string, array{string, string}>
     */
    public static function keySpellings(): array
    {
        $k = 'reports/'.self::PROJECT.'/20260929_012744_x.pdf';

        return [
            'as written' => [$k, $k],
            'leading slash' => ['/'.$k, $k],
            'logical bucket prefix' => ['bronze/'.$k, $k],
            's3 uri' => ['s3://georag-bronze-prod/'.$k, $k],
            'surrounding whitespace' => ["  {$k}\n", $k],
        ];
    }

    #[DataProvider('keySpellings')]
    public function test_object_keys_are_normalised(string $input, string $expected): void
    {
        $this->assertSame($expected, IngestionSnapshot::normaliseObjectKey($input));
    }

    public function test_a_differently_spelled_source_key_still_matches(): void
    {
        $runs = $this->snapshot()->assemble(
            [$this->report('r-1', 'Unrelated Title', 'bronze/'.self::MADSEN)],
            [],
            [$this->upload(self::MADSEN)],
        );

        $this->assertSame(0, $runs['totals']['in_flight']);
    }
}
