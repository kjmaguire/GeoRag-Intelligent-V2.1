<?php

declare(strict_types=1);

namespace App\Services;

use App\Http\Controllers\Api\V1\UploadController;
use App\Support\ExtractionMethods;
use App\Support\SetsWorkspaceRlsContext;
use Carbon\CarbonImmutable;
use Illuminate\Contracts\Cache\LockTimeoutException;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use League\Flysystem\StorageAttributes;

/**
 * Per-project ingestion snapshot — the ONE place that decides which uploaded
 * files are still ingesting.
 *
 * Two surfaces read it: the Ingestion Runs page (full snapshot, and its 5 s
 * JSON poll) and the Overview's "X files ingesting" tile (summary()). They
 * used to compute the number separately, and the Overview's copy never
 * looked at silver.ingest_progress at all: it counted every bronze object
 * whose filename did not START WITH the fingerprint of some report title.
 * An NI 43-101's stored title is the document's own extracted title, not its
 * upload filename, so a finished report never matched and the tile read
 * "10 files ingesting" indefinitely — then the frontend's poll replaced it
 * with the Runs page's (different) number 5-30 s later.
 *
 * MATCHING AN OBJECT TO ITS REPORT, in order of trust:
 *   1. silver.reports.source_object_key — ingest_pdf persists the exact
 *      bronze key it parsed (its `minio_key` input, which is the key
 *      UploadController wrote). Both sides are "{prefix}/{project_id}/
 *      {Ymd_His}_{name}" with no bucket and no leading "bronze/"; keys are
 *      normalised anyway (see normaliseObjectKey()).
 *   2. A TIFF/RRD upload under tiff/ never gets a report of its own:
 *      tiff_normalize derives "reports/{project_id}/tiff-derived-{sha8}-
 *      {stem}.pdf" and THAT key is the report's source_object_key. The
 *      stem is recoverable from the TIFF key, so it is matched on that.
 *   3. Title fingerprint — legacy only, and only against reports whose
 *      source_object_key is NULL (rows that predate 2026-08-19 and were
 *      missed by that migration's backfill). A report that knows its key is
 *      never matched by title, so a new upload cannot be "claimed" by an
 *      unrelated report whose title happens to share a prefix.
 *
 * Octane: no per-request state is held on the instance. Every method takes
 * the project/workspace it works on as arguments; the only dependency is
 * StorageService, which is itself stateless.
 */
final class IngestionSnapshot
{
    use SetsWorkspaceRlsContext;

    /**
     * TTL for the cached bronze upload listing. Short enough that a new
     * upload surfaces within about one 5-second poll cycle, long enough that
     * many open tabs share one S3 listing instead of each paying for it.
     */
    private const UPLOAD_LISTING_TTL_SECONDS = 8;

    /**
     * How long the last good listing is kept past its TTL, to answer callers
     * that lose the rebuild race and time out waiting for the lock.
     */
    private const UPLOAD_LISTING_STALE_SECONDS = 300;

    /** Lifetime of the rebuild lock; longer than any sane listing so a crashed worker cannot wedge it for long. */
    private const UPLOAD_LISTING_LOCK_SECONDS = 20;

    /** How long a caller that lost the lock waits for the winner before serving stale data. */
    private const UPLOAD_LISTING_LOCK_WAIT_SECONDS = 5;

    /**
     * Rows kept per section (reports, progress rows, bronze uploads), newest
     * first. build() runs on every 5 s poll of every open Ingestion Runs tab
     * and on each Overview load; unbounded it read every report, every
     * progress row and every bronze object of a project that has been fed
     * for years. The payload says when a section was cut (`truncated`).
     * Totals are computed over the rows kept.
     */
    public const MAX_ROWS_PER_SECTION = 500;

    /**
     * How long a NON-terminal progress row must have been quiet (no step
     * transition, no heartbeat — mark_heartbeat bumps updated_at every 30 s
     * while a task runs) before a report existing for its file overrides it.
     *
     * The report row is written at ingest_pdf's persist step, BEFORE the
     * embed steps, so "a report exists" alone does not mean the run is over:
     * overriding immediately would call a run idle while it is visibly still
     * embedding. Fifteen minutes is stale_run_detector's own window — past
     * it, the row is either a dead worker (Spot interruption, the 17:00
     * nightly stop) or waiting on the embed queue, and in both cases the
     * document is ingested.
     */
    private const REPORT_OVERRIDES_QUIET_ROW_AFTER_SECONDS = 900;

    /** Run steps / statuses that mean the run has stopped moving. */
    private const SETTLED_STATUSES = ['completed', 'partial', 'failed', 'cancelled', 'timed_out'];

    private const SETTLED_STEPS = ['completed', 'failed', 'cancelled', 'timed_out'];

    /**
     * @param int $listingLockWaitSeconds How long a caller that finds the
     *                                    upload listing being rebuilt waits for
     *                                    the rebuild before serving the stale
     *                                    copy. A constructor argument so a test
     *                                    can make it zero; the container uses
     *                                    the default.
     */
    public function __construct(
        private readonly StorageService $storage,
        private readonly int $listingLockWaitSeconds = self::UPLOAD_LISTING_LOCK_WAIT_SECONDS,
    ) {}

    /**
     * The Overview tile's numbers. Derived from build() — NOT a second
     * computation — so the server-rendered tile says exactly what the
     * frontend's poll of the Ingestion Runs JSON will say a few seconds
     * later.
     *
     * @return array{in_flight: int, completed: int, latest_in_flight: ?string}
     */
    public function summary(string $projectId, string $workspaceId): array
    {
        return self::summarise($this->build($projectId, $workspaceId));
    }

    /**
     * Project a snapshot onto the Overview tile's shape — the same three
     * values Overview.tsx reads out of the ingestion-runs.json poll.
     *
     * @param array{latest_in_flight: ?string, totals: array{in_flight: int, completed: int}} $snapshot
     *
     * @return array{in_flight: int, completed: int, latest_in_flight: ?string}
     */
    public static function summarise(array $snapshot): array
    {
        return [
            'in_flight' => $snapshot['totals']['in_flight'],
            'completed' => $snapshot['totals']['completed'],
            'latest_in_flight' => $snapshot['latest_in_flight'],
        ];
    }

    /**
     * Build the per-project ingestion snapshot.
     *
     * @return array{
     *     in_flight: list<array<string, mixed>>,
     *     completed: list<array<string, mixed>>,
     *     latest_in_flight: ?string,
     *     truncated: bool,
     *     truncated_sections: array{reports: bool, progress: bool, uploads: bool},
     *     totals: array{
     *         in_flight: int, completed: int, files: int,
     *         files_completed: int, files_partial: int, files_failed: int,
     *         files_timed_out: int, files_running: int,
     *     },
     * }
     */
    public function build(string $projectId, string $workspaceId): array
    {
        // Both callers (page load and the JSON poll) take the same path, and
        // so does the Overview tile. The listing is cached, see listUploads().
        $reports = $this->loadReports($projectId, $workspaceId);
        $progress = $this->loadProgressRows($projectId, $workspaceId);
        $uploads = $this->listUploads($projectId);

        $truncated = [
            'reports' => count($reports) > self::MAX_ROWS_PER_SECTION,
            'progress' => count($progress) > self::MAX_ROWS_PER_SECTION,
            'uploads' => count($uploads) > self::MAX_ROWS_PER_SECTION,
        ];

        return $this->assemble(
            array_slice($reports, 0, self::MAX_ROWS_PER_SECTION),
            array_slice($progress, 0, self::MAX_ROWS_PER_SECTION),
            array_slice($uploads, 0, self::MAX_ROWS_PER_SECTION),
            $truncated,
        );
    }

    /**
     * The pure half of build(): classify already-loaded rows. No I/O, so the
     * matching rules can be exercised without Postgres or an object store.
     *
     * @param list<array{report_id: string, title: string, source_object_key: ?string, parser_used: ?string, parse_quality_pct: ?float, text_page_coverage_pct: ?float, is_scanned: bool, passages: int, embedded: int}> $reports
     * @param list<array{minio_key: string, filename: string, current_step: string, step_index: int, total_steps: int, stage_pct: ?float, stage_detail: ?string, started_at: ?string, updated_at: ?string, failed_at: ?string, error_text: ?string, report_id: ?string, status: string, rows_written: ?int, warnings: list<array<string, mixed>>}> $progress
     * @param list<array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}> $uploads
     * @param array{reports?: bool, progress?: bool, uploads?: bool} $truncated Sections build() cut to MAX_ROWS_PER_SECTION.
     *
     * @return array{
     *     in_flight: list<array<string, mixed>>,
     *     completed: list<array<string, mixed>>,
     *     latest_in_flight: ?string,
     *     truncated: bool,
     *     truncated_sections: array{reports: bool, progress: bool, uploads: bool},
     *     totals: array{
     *         in_flight: int, completed: int, files: int,
     *         files_completed: int, files_partial: int, files_failed: int,
     *         files_timed_out: int, files_running: int,
     *     },
     * }
     */
    public function assemble(array $reports, array $progress, array $uploads, array $truncated = []): array
    {
        $index = $this->indexReports($reports);

        $uploadsByKey = [];
        foreach ($uploads as $u) {
            $uploadsByKey[self::normaliseObjectKey((string) $u['key'])] = $u;
        }

        $progressByKey = [];
        foreach ($progress as $p) {
            $progressByKey[self::normaliseObjectKey($p['minio_key'])] = true;
        }

        /**
         * Which bronze object each report came from, for the completed
         * card's filename / uploaded-at columns.
         *
         * @var array<string, array{filename: ?string, uploaded_at: ?string}> $sourceByReport
         */
        $sourceByReport = [];
        $rememberSource = function (string $reportId, string $key, ?string $fallbackAt) use (&$sourceByReport, $uploadsByKey): void {
            if (isset($sourceByReport[$reportId])) {
                return;
            }
            $upload = $uploadsByKey[self::normaliseObjectKey($key)] ?? null;
            $sourceByReport[$reportId] = [
                'filename' => $upload['filename'] ?? basename($key),
                'uploaded_at' => $upload['uploaded_at'] ?? $fallbackAt,
            ];
        };

        $inFlight = [];

        /**
         * Effective status per progress row, for the file ledger below.
         *
         * @var list<string> $ledgerStatuses
         */
        $ledgerStatuses = [];

        // 1. Real progress rows: anything not yet 'completed' is in flight.
        //    Terminal failures stay visible (warn pill + error text) for 24h
        //    so the user sees what broke, then drop off — without this cutoff
        //    every failed run ever stays pinned in "in flight" forever.
        foreach ($progress as $p) {
            // The row's own report_id is set by mark_report_id() only while
            // the run is still non-terminal, so a PDF that completed often
            // carries NULL here. The object key is the reliable link.
            $reportId = $p['report_id'] ?? $this->reportIdForObject($p['minio_key'], $index);
            if ($reportId !== null && isset($index['ids'][$reportId])) {
                $rememberSource($reportId, $p['minio_key'], $p['started_at']);
            }

            $ledgerStatus = $this->ledgerStatus($p);

            // A run that never reached a terminal state, for a file whose
            // report EXISTS, and that has been quiet past the stale window:
            // the document is ingested. Whatever stopped the row moving — a
            // worker killed mid-embed, a Spot interruption, the 17:00 nightly
            // stop before stale_run_detector got to it — it is not "still
            // ingesting", and counting it as such is what kept the Overview
            // tile reading "N files ingesting" after the reports had landed.
            if ($ledgerStatus === 'running'
                && $reportId !== null
                && isset($index['ids'][$reportId])
                && $this->quietForSeconds($p) >= self::REPORT_OVERRIDES_QUIET_ROW_AFTER_SECONDS) {
                $ledgerStatuses[] = 'completed';

                continue;
            }
            $ledgerStatuses[] = $ledgerStatus;

            // A 'partial' run also sets current_step='completed' — it DID
            // reach the end. It stays in this list anyway, because it is the
            // case the user most needs to see: the file finished processing
            // and produced nothing, or produced something and also
            // complained. Filtering on current_step alone is what made
            // "completed, zero rows written" render as an unqualified green
            // row with the explanation nowhere on the page.
            $isPartial = ($p['status'] ?? '') === 'partial';
            // A clean completion WITH a report row is rendered by the
            // completed card below (built from silver.reports), so it is
            // dropped here rather than said twice. A clean completion
            // WITHOUT one — a shapefile, a drill CSV, a LAS file — used to
            // be dropped by this same test, which erased the run from the
            // page at the moment it succeeded: the completed card is
            // reports-only, so success was the one outcome with no row
            // anywhere. It now stays, rendered green, for the same 24 h
            // window partial and failed rows already get.
            if ($p['current_step'] === 'completed' && ! $isPartial && $reportId !== null) {
                continue;
            }
            $settled = $isPartial
                || $p['current_step'] === 'completed'
                || in_array((string) $p['current_step'], self::SETTLED_STEPS, true);
            if ($settled
                && $p['started_at'] !== null
                && strtotime((string) $p['started_at']) < time() - 86400) {
                continue;
            }
            $inFlight[] = [
                'key' => $p['minio_key'],
                'filename' => $p['filename'],
                'size_bytes' => null,
                'uploaded_at' => $p['started_at'],
                'uploaded_ago' => $this->humanAgo($p['started_at']),
                'stage' => $p['current_step'],
                'stage_detail' => $p['stage_detail'] ?? null,
                'step_index' => $p['step_index'],
                'total_steps' => $p['total_steps'],
                // Smooth bar: completed steps + fractional progress within
                // the current step (stage_pct 0..1 written by the worker's
                // page-level relay). Falls back to the old step quantization
                // when the worker hasn't reported sub-step progress.
                //
                // A run whose current_step IS 'completed' has no current
                // step to be fractionally inside of — the formula rendered
                // every finished row at (n-1)/n, so "Finished with
                // warnings · 80%" sat on runs that had run to the end.
                'progress_pct' => $p['current_step'] === 'completed'
                    ? 100
                    : ($p['total_steps'] > 0
                        ? (int) round(
                            ((max(0, $p['step_index'] - 1) + (float) ($p['stage_pct'] ?? 0.0))
                                / $p['total_steps']) * 100,
                        )
                        : 0),
                'has_real_progress' => true,
                'failed' => $p['failed_at'] !== null,
                'error_text' => $p['error_text'],
                'status' => $p['status'],
                'rows_written' => $p['rows_written'] ?? null,
                'warnings' => $p['warnings'],
            ];
        }

        // 2. Fallback: bronze objects with no ingest_progress row at all —
        //    the window between upload and a worker picking the job up, and
        //    runs from before the instrumentation landed.
        foreach ($uploads as $u) {
            if (isset($progressByKey[self::normaliseObjectKey((string) $u['key'])])) {
                continue;  // already covered above
            }

            $reportId = $this->reportIdForObject((string) $u['key'], $index);
            if ($reportId !== null) {
                $rememberSource($reportId, (string) $u['key'], $u['uploaded_at']);

                continue;
            }

            $inFlight[] = [
                'key' => $u['key'],
                'filename' => $u['filename'],
                'size_bytes' => $u['size_bytes'],
                'uploaded_at' => $u['uploaded_at'],
                'uploaded_ago' => $this->humanAgo($u['uploaded_at']),
                'stage' => $this->guessStage($u['uploaded_at']),
                'step_index' => 0,
                'total_steps' => 5,
                'progress_pct' => 0,
                'has_real_progress' => false,
                'failed' => false,
                'error_text' => null,
                'status' => 'queued',
                'rows_written' => null,
                'warnings' => [],
            ];
        }

        $completedRows = [];
        foreach ($reports as $r) {
            $source = $sourceByReport[$r['report_id']] ?? null;

            $completedRows[] = [
                'report_id' => $r['report_id'],
                'title' => $r['title'],
                'parser_used' => $r['parser_used'],
                'parser_label' => ExtractionMethods::parserLabel($r['parser_used']),
                'parse_quality_pct' => $r['parse_quality_pct'],
                'text_page_coverage_pct' => $r['text_page_coverage_pct'],
                'is_scanned' => $r['is_scanned'],
                'passages' => $r['passages'],
                'embedded' => $r['embedded'],
                'embed_pct' => $r['passages'] > 0
                    ? (int) round(($r['embedded'] / $r['passages']) * 100)
                    : 0,
                'uploaded_at' => $source['uploaded_at'] ?? null,
                'uploaded_ago' => isset($source['uploaded_at'])
                    ? $this->humanAgo($source['uploaded_at'])
                    : null,
                'filename' => $source['filename'] ?? null,
            ];
        }

        // Newest first when we know the upload time; reports with no matched
        // upload sink to the bottom but stay in their relative order.
        usort($completedRows, function ($a, $b) {
            $ta = $a['uploaded_at'] ?? '';
            $tb = $b['uploaded_at'] ?? '';

            return strcmp($tb, $ta);
        });

        // Newest in-flight first too.
        usort($inFlight, function ($a, $b) {
            return strcmp($b['uploaded_at'] ?? '', $a['uploaded_at'] ?? '');
        });

        // Settled rows (completed / partial / failed…) ride in the same list
        // for their 24 h grace window, but they are not IN FLIGHT: the header
        // count, the stat tile and — importantly — the page's poll cadence
        // all read this total, and counting settled rows kept the 5 s fast
        // poll running for a day after everything had finished.
        $moving = array_values(array_filter(
            $inFlight,
            fn (array $r): bool => $this->isStillMoving($r),
        ));

        // Where every uploaded file ended up.
        //
        // The two totals above answer different questions and neither is
        // "how many of the files I dropped got processed":
        //
        //   in_flight  counts rows still MOVING — 0 once a delivery settles.
        //   completed  counts silver.reports rows, i.e. DOCUMENTS. A drill
        //              CSV, a shapefile and a .dbf produce no report at all,
        //              and one TIFF produces one (its normalised PDF).
        //
        // So a 72-file delivery could finish with the page reading "0 in
        // flight · 41 completed" and no number anywhere that added up to 72.
        // A real delivery did exactly that on 2026-08-25 and read as ~30
        // files silently lost. They were not: the run rows are keyed on
        // minio_key, and $progress is already DISTINCT ON (minio_key), so it
        // is one row per uploaded object and the honest denominator.
        //
        // Two reasons the file count still will not equal what the user
        // selected in the picker, both worth saying on the page rather than
        // leaving the user to discover:
        //   * a shapefile's .shp/.shx/.dbf/.prj are zipped into ONE upload;
        //   * a TIFF/RRD/JPEG ingests once as itself and once as the PDF it
        //     is normalised into, so it holds two keys.
        $byStatus = [
            'completed' => 0, 'partial' => 0, 'failed' => 0,
            'timed_out' => 0, 'cancelled' => 0, 'running' => 0,
        ];
        foreach ($ledgerStatuses as $status) {
            $byStatus[$status]++;
        }

        return [
            'in_flight' => $inFlight,
            'completed' => $completedRows,
            // The newest row that is actually still moving — not simply
            // in_flight[0], which may be a settled row in its 24 h window.
            'latest_in_flight' => isset($moving[0]) ? (string) $moving[0]['filename'] : null,
            // True when any section was cut to MAX_ROWS_PER_SECTION: older
            // rows exist that this payload does not show, and the totals
            // below cover only what it does.
            'truncated' => in_array(true, $truncated, true),
            'truncated_sections' => [
                'reports' => (bool) ($truncated['reports'] ?? false),
                'progress' => (bool) ($truncated['progress'] ?? false),
                'uploads' => (bool) ($truncated['uploads'] ?? false),
            ],
            'totals' => [
                'in_flight' => count($moving),
                'completed' => count($completedRows),
                // Files, not documents. `files` is the denominator the
                // per-status counts below add up to.
                'files' => count($progress),
                'files_completed' => $byStatus['completed'],
                'files_partial' => $byStatus['partial'],
                'files_failed' => $byStatus['failed'] + $byStatus['cancelled'],
                'files_timed_out' => $byStatus['timed_out'],
                'files_running' => $byStatus['running'],
            ],
        ];
    }

    /**
     * Normalise a bronze object key so the two sides of a match compare
     * equal regardless of how they were spelled.
     *
     * As built (2026-09-29) both sides already agree — UploadController
     * writes "{prefix}/{project_id}/{Ymd_His}_{name}" through a disk with no
     * root prefix, sends that exact string to FastAPI as `minio_key`, and
     * ingest_pdf persists it verbatim as source_object_key; georag_object_
     * storage passes keys to S3 unmodified. This guards the forms a future
     * writer could plausibly introduce: a leading slash, an s3:// URI, or
     * the logical bucket name as a leading "bronze/" segment.
     */
    public static function normaliseObjectKey(string $key): string
    {
        $key = trim($key);
        if (preg_match('#^[a-z][a-z0-9+.-]*://[^/]+/(.*)$#i', $key, $m) === 1) {
            $key = $m[1];
        }
        $key = ltrim($key, '/');
        if (str_starts_with($key, 'bronze/')) {
            $key = substr($key, strlen('bronze/'));
        }

        return $key;
    }

    /**
     * @param list<array{report_id: string, title: string, source_object_key: ?string}> $reports
     *
     * @return array{
     *     ids: array<string, true>,
     *     by_key: array<string, string>,
     *     by_tiff_stem: array<string, string>,
     *     legacy_title_fps: array<string, string>,
     * }
     */
    private function indexReports(array $reports): array
    {
        $index = ['ids' => [], 'by_key' => [], 'by_tiff_stem' => [], 'legacy_title_fps' => []];

        foreach ($reports as $r) {
            $reportId = $r['report_id'];
            $index['ids'][$reportId] = true;

            $sourceKey = $r['source_object_key'];
            if ($sourceKey === null || trim($sourceKey) === '') {
                $fp = $this->fingerprint($r['title']);
                if ($fp !== '') {
                    // Prefixed so an all-digit fingerprint cannot become an
                    // int array key (PHP coerces "12345" to 12345, and
                    // str_starts_with() then TypeErrors — caught 2026-05-25).
                    $index['legacy_title_fps']['t:'.$fp] = $reportId;
                }

                continue;
            }

            $normalised = self::normaliseObjectKey($sourceKey);
            $index['by_key'][$normalised] = $reportId;

            // tiff_normalize.derived_pdf_key():
            //   reports/{project_id}/tiff-derived-{sha256[:8]}-{safe_stem}.pdf
            if (preg_match('#^reports/[^/]+/tiff-derived-[0-9a-f]{8}-(.+)\.pdf$#', $normalised, $m) === 1) {
                $index['by_tiff_stem'][$m[1]] = $reportId;
            }
        }

        return $index;
    }

    /**
     * The report an uploaded object produced, or null if none has.
     *
     * @param array{ids: array<string, true>, by_key: array<string, string>, by_tiff_stem: array<string, string>, legacy_title_fps: array<string, string>} $index
     */
    private function reportIdForObject(string $key, array $index): ?string
    {
        $normalised = self::normaliseObjectKey($key);

        if (isset($index['by_key'][$normalised])) {
            return $index['by_key'][$normalised];
        }

        if (str_starts_with($normalised, 'tiff/')) {
            // Mirrors derived_pdf_key(): Path(key).stem, every run of
            // characters outside [A-Za-z0-9._-] collapsed to "_", first 80.
            $stem = pathinfo($normalised, PATHINFO_FILENAME);
            $safeStem = substr(preg_replace('/[^A-Za-z0-9._-]+/', '_', $stem) ?? $stem, 0, 80);
            if ($safeStem !== '' && isset($index['by_tiff_stem'][$safeStem])) {
                return $index['by_tiff_stem'][$safeStem];
            }
        }

        // Last resort, legacy rows only: does the filename (upload
        // timestamp and extension stripped) start with a report title?
        $fp = $this->fingerprint($this->stripFilename(basename($normalised)));
        if ($fp === '') {
            return null;
        }
        foreach ($index['legacy_title_fps'] as $prefixedTitleFp => $reportId) {
            if (str_starts_with($fp, substr((string) $prefixedTitleFp, 2))) {
                return $reportId;
            }
        }

        return null;
    }

    /**
     * The file-ledger bucket for one progress row.
     *
     * Both fields are consulted because rows from before the status column
     * existed (2026-08-21) carry current_step='completed' with a NULL
     * status — read by the mapper as 'queued', which would count a
     * long-finished run as moving forever.
     *
     * @param array<string, mixed> $p
     */
    private function ledgerStatus(array $p): string
    {
        $status = (string) ($p['status'] ?? '');
        if ($status === '' || $status === 'queued' || $status === 'started') {
            $status = in_array((string) $p['current_step'], self::SETTLED_STEPS, true)
                ? (string) $p['current_step']
                : 'running';
        }

        return in_array($status, ['completed', 'partial', 'failed', 'timed_out', 'cancelled'], true)
            ? $status
            : 'running';
    }

    /**
     * @param array<string, mixed> $row An in_flight row.
     */
    private function isStillMoving(array $row): bool
    {
        return ! in_array((string) $row['status'], self::SETTLED_STATUSES, true)
            && ! in_array((string) $row['stage'], self::SETTLED_STEPS, true)
            && ! (bool) $row['failed'];
    }

    /**
     * Seconds since a progress row last changed. updated_at is bumped by
     * every step transition and by the 30 s task heartbeat.
     *
     * @param array<string, mixed> $p
     */
    private function quietForSeconds(array $p): int
    {
        $last = $p['updated_at'] ?? $p['started_at'] ?? null;
        if ($last === null) {
            return PHP_INT_MAX;
        }
        $ts = strtotime((string) $last);

        return $ts === false ? PHP_INT_MAX : max(0, time() - $ts);
    }

    /**
     * @return list<array{
     *     report_id: string, title: string, source_object_key: ?string,
     *     parser_used: ?string, parse_quality_pct: ?float,
     *     text_page_coverage_pct: ?float, is_scanned: bool, passages: int,
     *     embedded: int,
     * }>
     */
    private function loadReports(string $projectId, string $workspaceId): array
    {
        // Perf audit 2026-08-15 (item 4) — the passage-count subquery used to
        // aggregate the WHOLE silver.document_passages table (every project,
        // every workspace) on every 5s poll tick, then throw away everything
        // that didn't match this project's report_ids in the outer join.
        // document_passages has no project_id column of its own (only
        // document_id + workspace_id — see the 2026-04-20 migration), so the
        // subquery is scoped by joining through silver.reports the same way
        // the outer query already does, before it ever aggregates.
        //
        // RLS fix 2026-08-15 (third pass): silver.document_passages was
        // converted to fail-closed, so the LEFT JOIN subquery above also
        // needs app.workspace_id bound or it silently contributes zero rows.
        //
        // Capped (2026-10-04): only the newest MAX_ROWS_PER_SECTION + 1 reports
        // are read (the extra row is how build() knows it cut something), and
        // the passage aggregate is restricted to exactly those reports.
        $rows = $this->withWorkspaceRls($workspaceId, fn () => DB::select(
            <<<'SQL'
            WITH recent AS (
                SELECT r.report_id, r.title, r.source_object_key, r.parser_used,
                       r.parse_quality_pct, r.text_page_coverage_pct, r.is_scanned
                FROM silver.reports r
                WHERE r.project_id = ?
                ORDER BY r.created_at DESC NULLS LAST, r.report_id
                LIMIT ?
            )
            SELECT
                r.report_id::text AS report_id,
                r.title,
                r.source_object_key,
                r.parser_used,
                r.parse_quality_pct,
                r.text_page_coverage_pct,
                r.is_scanned,
                COALESCE(p.passages, 0) AS passages,
                COALESCE(p.embedded, 0) AS embedded
            FROM recent r
            LEFT JOIN (
                SELECT dp.document_id,
                       COUNT(*) AS passages,
                       COUNT(*) FILTER (WHERE dp.embedding_id IS NOT NULL) AS embedded
                FROM silver.document_passages dp
                WHERE dp.document_id IN (SELECT report_id FROM recent)
                GROUP BY dp.document_id
            ) p ON p.document_id = r.report_id
            SQL,
            [$projectId, self::MAX_ROWS_PER_SECTION + 1],
        ));

        return array_map(static fn ($r) => [
            'report_id' => (string) $r->report_id,
            'title' => (string) $r->title,
            'source_object_key' => $r->source_object_key !== null ? (string) $r->source_object_key : null,
            'parser_used' => $r->parser_used,
            'parse_quality_pct' => $r->parse_quality_pct === null
                ? null
                : (float) $r->parse_quality_pct,
            // Extraction completeness, which is what the "Quality" column
            // used to be read as. parse_quality_pct above is NI 43-101
            // section-heading coverage and answers a different question;
            // null here means the row predates the column, not zero.
            'text_page_coverage_pct' => $r->text_page_coverage_pct === null
                ? null
                : (float) $r->text_page_coverage_pct,
            'is_scanned' => (bool) $r->is_scanned,
            'passages' => (int) $r->passages,
            'embedded' => (int) $r->embedded,
        ], $rows);
    }

    /**
     * Load real-time progress rows for the project from silver.ingest_progress.
     * Each row represents one file being processed by a Hatchet workflow,
     * with the current step + step index out of total.
     *
     * @return list<array{
     *     minio_key: string, filename: string, current_step: string,
     *     step_index: int, total_steps: int, stage_pct: ?float,
     *     stage_detail: ?string, started_at: ?string,
     *     updated_at: ?string, failed_at: ?string, error_text: ?string,
     *     report_id: ?string, status: string, rows_written: ?int,
     *     warnings: list<array<string, mixed>>,
     * }>
     */
    private function loadProgressRows(string $projectId, string $workspaceId): array
    {
        try {
            // Bound to the workspace like loadReports(). The policy on this
            // table is fail-open today, so this changes nothing yet — but it
            // is the difference between this page working and silently
            // reading zero rows the day the policy is made fail-closed.
            $rows = $this->withWorkspaceRls($workspaceId, fn () => DB::select(
                <<<'SQL'
                -- DISTINCT ON: retries/recovery sweeps create multiple rows
                -- per minio_key (attempt N + recovery rows); rendering every
                -- non-terminal one duplicated the same filename 3-10x in the
                -- in-flight list. Latest attempt wins.
                -- Capped: the outer query keeps the newest MAX_ROWS_PER_SECTION + 1
                -- runs (the extra row is how build() knows it cut something).
                SELECT minio_key, filename, current_step, step_index, total_steps,
                       stage_pct, stage_detail, started_at, updated_at, failed_at,
                       error_text, report_id, status, rows_written, warnings
                FROM (
                    SELECT DISTINCT ON (minio_key)
                           minio_key, filename, current_step,
                           step_index, total_steps,
                           stage_pct, stage_detail,
                           to_char(started_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS started_at,
                           to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS updated_at,
                           to_char(failed_at,  'YYYY-MM-DD"T"HH24:MI:SSOF') AS failed_at,
                           started_at AS sort_at,
                           error_text,
                           report_id::text AS report_id,
                           -- Added 2026-08-21. A run that reached the end having
                           -- written nothing used to render as an unqualified
                           -- green "Completed" while the warning explaining why
                           -- ("upload the collar file first") lived only inside
                           -- the Hatchet run object.
                           status,
                           rows_written,
                           warnings::text AS warnings
                    FROM silver.ingest_progress
                    WHERE project_id = ?
                    ORDER BY minio_key, attempt_number DESC, started_at DESC
                ) latest
                ORDER BY sort_at DESC NULLS LAST, minio_key
                LIMIT ?
                SQL,
                [$projectId, self::MAX_ROWS_PER_SECTION + 1],
            ));
        } catch (\Throwable $e) {
            return [];  // table absent in test envs that didn't run the migration
        }

        return array_map(static fn ($r) => [
            'minio_key' => (string) $r->minio_key,
            'filename' => (string) $r->filename,
            'current_step' => (string) $r->current_step,
            'step_index' => (int) $r->step_index,
            'total_steps' => (int) $r->total_steps,
            'stage_pct' => $r->stage_pct !== null ? (float) $r->stage_pct : null,
            'stage_detail' => $r->stage_detail !== null ? (string) $r->stage_detail : null,
            'started_at' => $r->started_at,
            'updated_at' => $r->updated_at,
            'failed_at' => $r->failed_at,
            'error_text' => $r->error_text,
            'report_id' => $r->report_id,
            'status' => (string) ($r->status ?? 'queued'),
            'rows_written' => $r->rows_written !== null ? (int) $r->rows_written : null,
            'warnings' => self::decodeWarnings($r->warnings ?? null),
        ], $rows);
    }

    /**
     * Decode the jsonb warnings array, tolerating anything unexpected.
     *
     * The column is NOT NULL DEFAULT '[]', but this page has to render for
     * rows written before the column existed and for a database that has
     * not run the migration yet — a malformed value must not take the
     * Ingestion Runs page down with it.
     *
     * @return list<array<string, mixed>>
     */
    private static function decodeWarnings(mixed $raw): array
    {
        if (! is_string($raw) || $raw === '') {
            return [];
        }

        $decoded = json_decode($raw, true);

        if (! is_array($decoded)) {
            return [];
        }

        return array_values(array_filter($decoded, 'is_array'));
    }

    /**
     * List bronze objects under every upload prefix for this project.
     * Returns each object's key, derived filename, size, and uploaded_at.
     *
     * @return list<array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}>
     */
    private function listUploads(string $projectId): array
    {
        // Cached because this is the expensive part of the snapshot.
        //
        // That cost is why 2f332f2 (2026-08-11) dropped the listing from the
        // 5-second poll entirely — but doing so broke the page. The fallback
        // in build() turns an unmatched bronze object into an in_flight
        // row, and there is a real window between upload and the first
        // silver.ingest_progress row (Laravel dispatches to Hatchet; the row
        // is written by FastAPI's _progress.start_run() only once a worker
        // actually picks the job up). During that window a just-uploaded file
        // rendered on page load and then DISAPPEARED on the next poll — and
        // because in_flight then read 0, the UI's own backoff dropped it from
        // a 5s to a 30s poll, so the real progress row took up to 30s to
        // appear. Refreshing brought the file back, which is exactly the
        // "I have to reload it myself" symptom.
        //
        // A short shared TTL fixes both: correctness is restored, and the S3
        // cost drops from (one listing x every open tab / 5s) to one listing
        // per TTL window TOTAL, since the cache is shared across tabs,
        // requests and — since 2026-09-29 — the Overview tile. Keyed by
        // project so nothing leaks across tenants.
        //
        // TTL is deliberately shorter than the 5s poll x 2 so a new upload
        // surfaces within roughly one poll cycle of landing in bronze.
        //
        // Cache::remember() alone has no stampede protection: when the entry
        // expires, every request in flight at that moment misses together and
        // each runs the whole S3 listing. A lock lets one caller rebuild
        // while the rest wait for its result; a caller that times out waiting
        // is served the previous listing (kept STALE_SECONDS longer) rather
        // than starting a listing of its own.
        $key = "ingestion-runs:uploads:{$projectId}";
        $cached = Cache::get($key);
        if (is_array($cached)) {
            return $cached;
        }

        $lock = Cache::lock($key.':lock', self::UPLOAD_LISTING_LOCK_SECONDS);

        try {
            $lock->block($this->listingLockWaitSeconds);
        } catch (LockTimeoutException) {
            $stale = Cache::get($key.':stale');
            if (is_array($stale)) {
                return $stale;
            }

            // Nothing to serve: list once ourselves, uncached.
            return $this->listUploadsUncached($projectId);
        }

        try {
            // The caller that held the lock has probably just filled it.
            $cached = Cache::get($key);
            if (is_array($cached)) {
                return $cached;
            }

            $uploads = $this->listUploadsUncached($projectId);
            Cache::put($key, $uploads, now()->addSeconds(self::UPLOAD_LISTING_TTL_SECONDS));
            Cache::put($key.':stale', $uploads, now()->addSeconds(self::UPLOAD_LISTING_STALE_SECONDS));

            return $uploads;
        } finally {
            $lock->release();
        }
    }

    /**
     * One directory listing per upload prefix, newest first, capped.
     *
     * This used to call files() and then size() and lastModified() on every
     * object — ~16 prefix listings plus two HEAD requests per object, so a
     * 200-report project cost ~400 S3 calls per rebuild. listContents()
     * returns each object's size and last-modified time in the listing
     * response itself, so the cost is one request per prefix however many
     * objects it holds.
     *
     * The cap is MAX_ROWS_PER_SECTION + 1 (the extra row is how build()
     * knows the listing was cut), applied after sorting by upload time.
     *
     * @return list<array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}>
     */
    private function listUploadsUncached(string $projectId): array
    {
        $disk = $this->storage->bronzeReadOnly();
        $out = [];

        // Every prefix an upload can land under, not just the two PDF ones.
        // Scanning only reports/ + tiff/ meant a CSV, XLSX, shapefile,
        // GeoPackage, QGIS project or LAS file had no fallback row here — so
        // when its progress row was also missing, the upload was invisible on
        // this page in both directions, success and failure alike.
        foreach (UploadController::bronzePrefixes() as $prefix) {
            try {
                /** @var iterable<StorageAttributes> $listing */
                $listing = $disk->getDriver()->listContents("{$prefix}/{$projectId}", false);

                foreach ($listing as $item) {
                    if (! $item->isFile()) {
                        continue;
                    }

                    $modified = $item->lastModified();
                    $size = method_exists($item, 'fileSize') ? $item->fileSize() : null;

                    $out[] = [
                        'key' => (string) $item->path(),
                        'filename' => basename((string) $item->path()),
                        'size_bytes' => $size === null ? null : (int) $size,
                        'uploaded_at' => $modified !== null
                            ? CarbonImmutable::createFromTimestamp($modified)->toIso8601String()
                            : null,
                    ];
                }
            } catch (\Throwable $e) {
                Log::debug('ingestion snapshot: bronze prefix listing failed', [
                    'prefix' => $prefix,
                    'error' => $e->getMessage(),
                ]);
            }
        }

        usort($out, fn (array $a, array $b): int => strcmp((string) ($b['uploaded_at'] ?? ''), (string) ($a['uploaded_at'] ?? '')));

        return array_slice($out, 0, self::MAX_ROWS_PER_SECTION + 1);
    }

    /**
     * Strip the upload-timestamp prefix and extension from a filename.
     * Example: "20260524_212637_Madsen_PFS.pdf" → "Madsen_PFS".
     */
    private function stripFilename(string $filename): string
    {
        $stem = pathinfo($filename, PATHINFO_FILENAME);
        // Match the "YYYYMMDD_HHMMSS_" prefix written by UploadController.
        $stem = preg_replace('/^\d{8}_\d{6}_/', '', $stem) ?? $stem;

        return $stem;
    }

    /**
     * Normalise a string for loose prefix matching between legacy report
     * titles and upload filenames (lowercased, alnum only).
     */
    private function fingerprint(string $value): string
    {
        $lower = strtolower($value);
        $alnum = preg_replace('/[^a-z0-9]+/', '', $lower) ?? '';

        return substr($alnum, 0, 40);
    }

    /**
     * Heuristic stage guess for files with no progress row yet, based on
     * elapsed time since upload.
     */
    private function guessStage(?string $uploadedAt): string
    {
        if ($uploadedAt === null) {
            return 'queued';
        }
        $age = (int) abs(CarbonImmutable::now()->diffInSeconds(CarbonImmutable::parse($uploadedAt)));
        if ($age < 30) {
            return 'queued';
        }
        if ($age < 120) {
            return 'parsing';
        }
        if ($age < 600) {
            return 'extracting tables';
        }

        return 'embedding';
    }

    private function humanAgo(?string $iso): ?string
    {
        if ($iso === null) {
            return null;
        }
        try {
            return CarbonImmutable::parse($iso)->diffForHumans();
        } catch (\Throwable $e) {
            return null;
        }
    }
}
