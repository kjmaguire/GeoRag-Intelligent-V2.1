<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Api\V1\UploadController;
use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\StorageService;
use App\Support\SetsWorkspaceRlsContext;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/IngestionRunsController — per-project ingestion progress surface.
 *
 * Phase A (this controller): derives state from existing tables + the bronze
 * MinIO listing. No new schema, no Hatchet step instrumentation. Phase B will
 * replace the in-flight derivation with a `silver.ingest_progress` table the
 * Hatchet steps write to.
 *
 * Two endpoints:
 *   GET /projects/{slug}/ingestion-runs        → Inertia view
 *   GET /projects/{slug}/ingestion-runs.json   → JSON for the 5s poll
 */
class IngestionRunsController extends Controller
{
    use SetsWorkspaceRlsContext;

    /**
     * TTL for the cached bronze upload listing. Short enough that a new
     * upload surfaces within about one 5-second poll cycle, long enough that
     * many open tabs share one S3 listing instead of each paying for it.
     */
    private const UPLOAD_LISTING_TTL_SECONDS = 8;

    public function __construct(
        private readonly StorageService $storage,
    ) {}

    public function show(Request $request, string $slug): Response
    {
        $project = $this->loadProject($request, $slug);

        return Inertia::render('Foundry/IngestionRuns', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'runs' => $this->buildSnapshot($project->project_id, (string) $project->workspace_id),
        ]);
    }

    public function progress(Request $request, string $slug): JsonResponse
    {
        $project = $this->loadProject($request, $slug);

        return response()->json([
            // The poll DOES include the bronze upload listing — see
            // listUploads()'s cache. Skipping it (as this endpoint did
            // between 2f332f2 and 2026-08-19) made a freshly-uploaded file
            // appear on page load and then vanish on the very next poll.
            'runs' => $this->buildSnapshot(
                $project->project_id,
                (string) $project->workspace_id,
            ),
            'fetched_at' => CarbonImmutable::now()->toIso8601String(),
        ]);
    }

    private function loadProject(Request $request, string $slug): Project
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()
            ->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        return $project;
    }

    /**
     * Build the per-project ingestion snapshot.
     *
     * @return array{
     *     in_flight: list<array<string, mixed>>,
     *     completed: list<array<string, mixed>>,
     *     totals: array<string, int>,
     * }
     */
    private function buildSnapshot(string $projectId, string $workspaceId): array
    {
        $reports = $this->loadReports($projectId, $workspaceId);
        $progress = $this->loadProgressRows($projectId);
        // Both callers (page load and the JSON poll) now take the same path.
        // The $includeUploadListing toggle that used to let progress() skip
        // this is gone: it was the flicker, and the cache removes the cost
        // that motivated it.
        $uploads = $this->listUploads($projectId);

        // Phase B: prefer real progress rows from silver.ingest_progress. Build
        // a set of MinIO keys we already have authoritative state for so we
        // don't double-count via the Phase-A fingerprint heuristic.
        $progressByKey = [];
        foreach ($progress as $p) {
            $progressByKey[$p['minio_key']] = $p;
        }

        // Build a set of report titles (lowercased, normalised) for matching
        // against uploaded filenames. Filenames are typically of the form
        // "20260524_212637_Foo_Bar_Report.pdf" — strip the date+time prefix
        // and the extension to compare. Only used for runs that pre-date the
        // ingest_progress instrumentation.
        $titleHashes = [];
        foreach ($reports as $r) {
            $key = $this->fingerprint((string) $r['title']);
            if ($key !== '') {
                $titleHashes[$key] = true;
            }
        }

        $inFlight = [];
        $matched = [];

        // 1. Real progress rows: anything not yet 'completed' is in flight.
        //    Terminal failures stay visible (warn pill + error text) for 24h
        //    so the user sees what broke, then drop off — without this cutoff
        //    every failed run ever stays pinned in "in flight" forever.
        $terminalSteps = ['failed', 'cancelled', 'timed_out'];
        foreach ($progress as $p) {
            // A 'partial' run also sets current_step='completed' — it DID
            // reach the end. It stays in this list anyway, because it is the
            // case the user most needs to see: the file finished processing
            // and produced nothing, or produced something and also
            // complained. Filtering on current_step alone is what made
            // "completed, zero rows written" render as an unqualified green
            // row with the explanation nowhere on the page.
            $isPartial = ($p['status'] ?? '') === 'partial';
            if ($p['current_step'] === 'completed' && ! $isPartial) {
                continue;
            }
            if (($isPartial || in_array((string) $p['current_step'], $terminalSteps, true))
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
                'progress_pct' => $p['total_steps'] > 0
                    ? (int) round(
                        ((max(0, $p['step_index'] - 1) + (float) ($p['stage_pct'] ?? 0.0))
                            / $p['total_steps']) * 100,
                    )
                    : 0,
                'has_real_progress' => true,
                'failed' => $p['failed_at'] !== null,
                'error_text' => $p['error_text'],
                'status' => $p['status'] ?? 'started',
                'rows_written' => $p['rows_written'] ?? null,
                'warnings' => $p['warnings'] ?? [],
            ];
        }

        // 2. Phase-A fallback: MinIO uploads that have no ingest_progress row
        //    (legacy runs from before the instrumentation landed).
        foreach ($uploads as $u) {
            if (isset($progressByKey[$u['key']])) {
                continue;  // already covered above
            }

            $stem = $this->stripFilename($u['filename']);
            $fp = $this->fingerprint($stem);

            $isMatched = false;
            foreach ($titleHashes as $titleFp => $_) {
                // PHP coerces all-numeric array keys to int, so a
                // fingerprint like "12345" comes back as int 12345
                // here — str_starts_with() then TypeErrors. Cast to
                // string defensively. (Caught 2026-05-25 on
                // /projects/cameco-shirley-basin where some report
                // titles fingerprint to numeric-only strings.)
                $titleFp = (string) $titleFp;
                if ($titleFp !== '' && str_starts_with($fp, $titleFp)) {
                    $isMatched = true;
                    $matched[$titleFp] = $u;
                    break;
                }
            }

            if (! $isMatched) {
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
        }

        $completedRows = [];
        foreach ($reports as $r) {
            $key = $this->fingerprint((string) $r['title']);
            $match = $matched[$key] ?? null;

            $completedRows[] = [
                'report_id' => $r['report_id'],
                'title' => $r['title'],
                'parser_used' => $r['parser_used'],
                'parse_quality_pct' => $r['parse_quality_pct'],
                'is_scanned' => $r['is_scanned'],
                'passages' => $r['passages'],
                'embedded' => $r['embedded'],
                'embed_pct' => $r['passages'] > 0
                    ? (int) round(($r['embedded'] / $r['passages']) * 100)
                    : 0,
                'uploaded_at' => $match['uploaded_at'] ?? null,
                'uploaded_ago' => isset($match['uploaded_at'])
                    ? $this->humanAgo($match['uploaded_at'])
                    : null,
                'filename' => $match['filename'] ?? null,
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

        return [
            'in_flight' => $inFlight,
            'completed' => $completedRows,
            'totals' => [
                'in_flight' => count($inFlight),
                'completed' => count($completedRows),
            ],
        ];
    }

    /**
     * @return list<array{
     *     report_id: string, title: string, parser_used: ?string,
     *     parse_quality_pct: ?float, is_scanned: bool, passages: int, embedded: int,
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
        $rows = $this->withWorkspaceRls($workspaceId, fn () => DB::select(
            <<<'SQL'
            SELECT
                r.report_id::text AS report_id,
                r.title,
                r.parser_used,
                r.parse_quality_pct,
                r.is_scanned,
                COALESCE(p.passages, 0) AS passages,
                COALESCE(p.embedded, 0) AS embedded
            FROM silver.reports r
            LEFT JOIN (
                SELECT dp.document_id,
                       COUNT(*) AS passages,
                       COUNT(*) FILTER (WHERE dp.embedding_id IS NOT NULL) AS embedded
                FROM silver.document_passages dp
                JOIN silver.reports r2 ON r2.report_id = dp.document_id
                WHERE r2.project_id = ?
                GROUP BY dp.document_id
            ) p ON p.document_id = r.report_id
            WHERE r.project_id = ?
            SQL,
            [$projectId, $projectId],
        ));

        return array_map(static fn ($r) => [
            'report_id' => (string) $r->report_id,
            'title' => (string) $r->title,
            'parser_used' => $r->parser_used,
            'parse_quality_pct' => $r->parse_quality_pct === null
                ? null
                : (float) $r->parse_quality_pct,
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
     *     step_index: int, total_steps: int, started_at: ?string,
     *     updated_at: ?string, failed_at: ?string, error_text: ?string,
     *     report_id: ?string, status: string, rows_written: ?int,
     *     warnings: list<array<string, mixed>>,
     * }>
     */
    private function loadProgressRows(string $projectId): array
    {
        try {
            $rows = DB::select(
                <<<'SQL'
                -- DISTINCT ON: retries/recovery sweeps create multiple rows
                -- per minio_key (attempt N + recovery rows); rendering every
                -- non-terminal one duplicated the same filename 3-10x in the
                -- in-flight list. Latest attempt wins.
                SELECT DISTINCT ON (minio_key)
                       minio_key, filename, current_step,
                       step_index, total_steps,
                       stage_pct, stage_detail,
                       to_char(started_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS started_at,
                       to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS updated_at,
                       to_char(failed_at,  'YYYY-MM-DD"T"HH24:MI:SSOF') AS failed_at,
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
                SQL,
                [$projectId],
            );
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
     * List MinIO objects under bronze/reports/{project_id}/ and bronze/tiff/{project_id}/.
     * Returns each object's key, derived filename, size, and uploaded_at.
     *
     * @return list<array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}>
     */
    private function listUploads(string $projectId): array
    {
        // Cached because this is the expensive part of the snapshot: ~2 S3
        // round-trips (size + lastModified) per object, so a 200-report
        // project costs ~400 calls per uncached build.
        //
        // That cost is why 2f332f2 (2026-08-11) dropped the listing from the
        // 5-second poll entirely — but doing so broke the page. The Phase-A
        // fallback below turns an unmatched bronze object into an in_flight
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
        // cost drops from (400 calls x every open tab / 5s) to 400 calls per
        // TTL window TOTAL, since the cache is shared across tabs and
        // requests. Keyed by project so nothing leaks across tenants.
        //
        // TTL is deliberately shorter than the 5s poll x 2 so a new upload
        // surfaces within roughly one poll cycle of landing in bronze.
        return Cache::remember(
            "ingestion-runs:uploads:{$projectId}",
            now()->addSeconds(self::UPLOAD_LISTING_TTL_SECONDS),
            fn (): array => $this->listUploadsUncached($projectId),
        );
    }

    /**
     * @return list<array<string, mixed>>
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
                $keys = $disk->files("{$prefix}/{$projectId}");
            } catch (\Throwable $e) {
                $keys = [];
            }

            foreach ($keys as $key) {
                try {
                    $size = $disk->size($key);
                } catch (\Throwable $e) {
                    $size = null;
                }
                try {
                    $modified = $disk->lastModified($key);
                    $uploadedAt = $modified
                        ? CarbonImmutable::createFromTimestamp($modified)->toIso8601String()
                        : null;
                } catch (\Throwable $e) {
                    $uploadedAt = null;
                }

                $out[] = [
                    'key' => $key,
                    'filename' => basename($key),
                    'size_bytes' => $size === null ? null : (int) $size,
                    'uploaded_at' => $uploadedAt,
                ];
            }
        }

        return $out;
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
     * Normalise a string for loose substring matching between report titles
     * and upload filenames (lowercased, alnum only).
     */
    private function fingerprint(string $value): string
    {
        $lower = strtolower($value);
        $alnum = preg_replace('/[^a-z0-9]+/', '', $lower) ?? '';

        return substr($alnum, 0, 40);
    }

    /**
     * Heuristic stage guess for in-flight files based on elapsed time since
     * upload. Replaced by real per-step status in Phase B.
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
