<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
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
    public function show(Request $request, string $slug): Response
    {
        $project = $this->loadProject($request, $slug);

        return Inertia::render('Foundry/IngestionRuns', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'runs' => $this->buildSnapshot($project->project_id),
        ]);
    }

    public function progress(Request $request, string $slug): JsonResponse
    {
        $project = $this->loadProject($request, $slug);

        return response()->json([
            'runs' => $this->buildSnapshot($project->project_id),
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
    private function buildSnapshot(string $projectId): array
    {
        $reports = $this->loadReports($projectId);
        $progress = $this->loadProgressRows($projectId);
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
        foreach ($progress as $p) {
            if ($p['current_step'] === 'completed') {
                continue;
            }
            $inFlight[] = [
                'key' => $p['minio_key'],
                'filename' => $p['filename'],
                'size_bytes' => null,
                'uploaded_at' => $p['started_at'],
                'uploaded_ago' => $this->humanAgo($p['started_at']),
                'stage' => $p['current_step'],
                'step_index' => $p['step_index'],
                'total_steps' => $p['total_steps'],
                'progress_pct' => $p['total_steps'] > 0
                    ? (int) round(($p['step_index'] / $p['total_steps']) * 100)
                    : 0,
                'has_real_progress' => true,
                'failed' => $p['failed_at'] !== null,
                'error_text' => $p['error_text'],
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
    private function loadReports(string $projectId): array
    {
        $rows = DB::select(
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
                SELECT document_id,
                       COUNT(*) AS passages,
                       COUNT(*) FILTER (WHERE embedding_id IS NOT NULL) AS embedded
                FROM silver.document_passages
                GROUP BY document_id
            ) p ON p.document_id = r.report_id
            WHERE r.project_id = ?
            SQL,
            [$projectId],
        );

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
     *     report_id: ?string,
     * }>
     */
    private function loadProgressRows(string $projectId): array
    {
        try {
            $rows = DB::select(
                <<<'SQL'
                SELECT minio_key, filename, current_step,
                       step_index, total_steps,
                       to_char(started_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS started_at,
                       to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSOF') AS updated_at,
                       to_char(failed_at,  'YYYY-MM-DD"T"HH24:MI:SSOF') AS failed_at,
                       error_text,
                       report_id::text AS report_id
                FROM silver.ingest_progress
                WHERE project_id = ?
                ORDER BY updated_at DESC
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
            'started_at' => $r->started_at,
            'updated_at' => $r->updated_at,
            'failed_at' => $r->failed_at,
            'error_text' => $r->error_text,
            'report_id' => $r->report_id,
        ], $rows);
    }

    /**
     * List MinIO objects under bronze/reports/{project_id}/ and bronze/tiff/{project_id}/.
     * Returns each object's key, derived filename, size, and uploaded_at.
     *
     * @return list<array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}>
     */
    private function listUploads(string $projectId): array
    {
        $disk = Storage::disk('s3-bronze');
        $out = [];

        foreach (['reports', 'tiff'] as $prefix) {
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
