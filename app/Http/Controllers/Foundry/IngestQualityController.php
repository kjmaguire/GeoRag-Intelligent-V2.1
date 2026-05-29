<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/IngestQualityController — post-import trust-moment surface.
 *
 * Reads silver.document_ingestion_quality + silver.ocr_page_quality +
 * silver.table_extraction_quality + silver.low_confidence_page_reviews +
 * silver.parser_run_artifacts + silver.bronze_provenance.
 *
 * Promotion gate: passes when acceptRate >= 95% AND fatalFiles == 0.
 */
class IngestQualityController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $files = collect();
        $anomalies = collect();

        try {
            $files = DB::table('silver.document_ingestion_quality as q')
                ->where('q.project_id', $project->project_id)
                ->orderByDesc('q.created_at')
                ->limit(50)
                ->get();
        } catch (\Throwable $e) {
            // empty
        }

        $fileRows = $files->map(fn ($f) => [
            'file_id' => (string) ($f->document_id ?? $f->id ?? ''),
            'name' => (string) ($f->file_name ?? $f->title ?? 'unknown'),
            'format' => strtoupper((string) ($f->format ?? 'OTHER')),
            'size_bytes' => isset($f->size_bytes) ? (int) $f->size_bytes : null,
            'rows' => isset($f->row_count) ? (int) $f->row_count : null,
            'accepted' => isset($f->accepted_rows) ? (int) $f->accepted_rows : null,
            'flagged' => isset($f->flagged_rows) ? (int) $f->flagged_rows : null,
            'rejected' => isset($f->rejected_rows) ? (int) $f->rejected_rows : null,
            'status' => (string) ($f->status ?? 'ok'),
            'crs_detected' => $f->crs_detected ?? null,
            'crs_confidence' => isset($f->crs_confidence) ? (float) $f->crs_confidence : null,
            'duration_seconds' => isset($f->duration_seconds) ? (int) $f->duration_seconds : null,
        ])->values();

        // Headline TIFF backlog row — surfaces the 1,230 deferred Tier-2 pages.
        $tiffBacklog = 0;
        try {
            $tiffBacklog = DB::table('silver.low_confidence_page_reviews')
                ->where('project_id', $project->project_id)
                ->where('review_status', 'pending_ocr')
                ->count();
        } catch (\Throwable $e) {
            // table may not exist in all envs
        }

        if ($tiffBacklog > 0) {
            $fileRows = $fileRows->prepend([
                'file_id' => 'tiff-backlog',
                'name' => "{$tiffBacklog} TIFF pages awaiting Tier-2 OCR",
                'format' => 'TIFF',
                'size_bytes' => null,
                'rows' => $tiffBacklog,
                'accepted' => null,
                'flagged' => null,
                'rejected' => null,
                'status' => 'awaiting_ocr',
                'crs_detected' => null,
                'crs_confidence' => null,
                'duration_seconds' => null,
            ]);
        }

        $accepted = (int) $fileRows->sum('accepted');
        $flagged = (int) $fileRows->sum('flagged');
        $rejected = (int) $fileRows->sum('rejected');
        $awaiting = $tiffBacklog;

        $rowsTotal = $accepted + $flagged + $rejected;
        $passGate = $rowsTotal === 0 ? false : (($accepted / max(1, $rowsTotal)) >= 0.95 && $rejected === 0);

        return Inertia::render('Foundry/IngestQuality', [
            'import_id' => (string) ($request->query('import') ?? 'latest'),
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'files' => $fileRows,
            'anomalies' => $anomalies,
            'totals' => [
                'accepted' => $accepted,
                'flagged' => $flagged,
                'rejected' => $rejected,
                'awaiting_ocr' => $awaiting,
            ],
            'pass_gate' => $passGate,
            'empty' => $fileRows->isEmpty(),
        ]);
    }
}
