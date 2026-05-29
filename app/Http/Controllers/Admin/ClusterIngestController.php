<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Doc-phase 183 — Cluster ingest dashboard.
 *
 * Surfaces the state of Phase A/B/C/D ingestion for large-archive
 * processing (e.g. Uranium_Logs_ALL.zip):
 *
 *   - bronze.ingest_runs (Phase A inspection walks)
 *   - bronze.ingest_manifest (per-file inventory)
 *   - silver.* state per project (collars / passages / reports)
 *   - Neo4j sync state (proxy: silver.collars vs Hatchet workflow run)
 *   - Qdrant embedding state (silver.document_passages.embedding_id)
 *
 * Read-only. Triggers (Hatchet workflow invocations) come from a
 * sibling controller in a future tick.
 *
 * Route: GET /admin/cluster-ingest
 * Auth: 'admin' Gate.
 */
class ClusterIngestController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        return Inertia::render('Admin/ClusterIngest', [
            'kpis' => $this->kpis(),
            'recent_runs' => $this->recentIngestRuns(),
            'top_clusters' => $this->topClusters(),
            'per_project' => $this->perProjectState(),
        ]);
    }

    /**
     * Top-level KPI cards.
     *
     * @return array{
     *   total_ingest_runs: int,
     *   total_files_indexed: int,
     *   total_bytes_indexed: int,
     *   total_collars: int,
     *   total_well_log_curves: int,
     *   total_passages: int,
     *   passages_embedded: int,
     *   passages_pending_embed: int,
     * }
     */
    private function kpis(): array
    {
        $row = DB::selectOne(<<<'SQL'
            SELECT
              (SELECT count(*) FROM bronze.ingest_runs)                        AS runs,
              (SELECT sum(files_indexed) FROM bronze.ingest_runs WHERE status='completed') AS files,
              (SELECT sum(bytes_seen)    FROM bronze.ingest_runs WHERE status='completed') AS bytes,
              (SELECT count(*) FROM silver.collars)                            AS collars,
              (SELECT count(*) FROM silver.well_log_curves)                    AS curves,
              (SELECT count(*) FROM silver.document_passages)                  AS passages,
              (SELECT count(*) FROM silver.document_passages WHERE embedding_id IS NOT NULL)   AS embedded,
              (SELECT count(*) FROM silver.document_passages WHERE embedding_id IS NULL)       AS pending
        SQL);

        return [
            'total_ingest_runs' => (int) ($row->runs ?? 0),
            'total_files_indexed' => (int) ($row->files ?? 0),
            'total_bytes_indexed' => (int) ($row->bytes ?? 0),
            'total_collars' => (int) ($row->collars ?? 0),
            'total_well_log_curves' => (int) ($row->curves ?? 0),
            'total_passages' => (int) ($row->passages ?? 0),
            'passages_embedded' => (int) ($row->embedded ?? 0),
            'passages_pending_embed' => (int) ($row->pending ?? 0),
        ];
    }

    /**
     * Recent Phase A ingest runs.
     *
     * @return array<int, array{
     *   run_id: string,
     *   source_path: string,
     *   status: string,
     *   started_at: string,
     *   completed_at: ?string,
     *   files_indexed: int,
     *   bytes_seen: int,
     *   summary: array,
     * }>
     */
    private function recentIngestRuns(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT run_id::text AS run_id, source_path, status,
                   started_at, completed_at,
                   files_seen, files_indexed,
                   bytes_seen,
                   summary_payload
              FROM bronze.ingest_runs
             ORDER BY started_at DESC
             LIMIT 15
        SQL);

        return array_map(static fn (object $r) => [
            'run_id' => (string) $r->run_id,
            'source_path' => $r->source_path,
            'status' => $r->status,
            'started_at' => $r->started_at,
            'completed_at' => $r->completed_at,
            'files_seen' => (int) $r->files_seen,
            'files_indexed' => (int) $r->files_indexed,
            'bytes_seen' => (int) $r->bytes_seen,
            'summary' => $r->summary_payload
                ? json_decode($r->summary_payload, true)
                : [],
        ], $rows);
    }

    /**
     * Top clusters by file count across all ingest_runs.
     *
     * @return array<int, array{cluster_key: string, file_count: int, total_bytes: int}>
     */
    private function topClusters(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT cluster_key, count(*) AS file_count,
                   sum(file_size_bytes)::bigint AS total_bytes
              FROM bronze.ingest_manifest
             WHERE cluster_key IS NOT NULL
             GROUP BY cluster_key
             ORDER BY count(*) DESC
             LIMIT 25
        SQL);

        return array_map(static fn (object $r) => [
            'cluster_key' => $r->cluster_key,
            'file_count' => (int) $r->file_count,
            'total_bytes' => (int) $r->total_bytes,
        ], $rows);
    }

    /**
     * Per-project ingestion state — collars, curves, passages, embeddings.
     *
     * @return array<int, array{
     *   project_id: string,
     *   project_name: string,
     *   slug: string,
     *   collar_count: int,
     *   curve_count: int,
     *   passage_count: int,
     *   embedded_count: int,
     *   embedding_pct: float,
     * }>
     */
    private function perProjectState(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT p.project_id::text AS project_id,
                   p.project_name, p.slug, p.commodity, p.region,
                   (SELECT count(*) FROM silver.collars c WHERE c.project_id = p.project_id)
                       AS collar_count,
                   (SELECT count(*) FROM silver.well_log_curves wc
                     JOIN silver.collars c ON wc.collar_id = c.collar_id
                    WHERE c.project_id = p.project_id) AS curve_count,
                   (SELECT count(*) FROM silver.document_passages dp
                     JOIN silver.reports r ON dp.document_id = r.report_id
                    WHERE r.project_id = p.project_id) AS passage_count,
                   (SELECT count(*) FROM silver.document_passages dp
                     JOIN silver.reports r ON dp.document_id = r.report_id
                    WHERE r.project_id = p.project_id AND dp.embedding_id IS NOT NULL)
                       AS embedded_count
              FROM silver.projects p
             ORDER BY p.created_at ASC
        SQL);

        return array_map(static function (object $r): array {
            $passages = (int) $r->passage_count;
            $embedded = (int) $r->embedded_count;
            $pct = $passages > 0 ? round(($embedded / $passages) * 100, 1) : 0.0;
            return [
                'project_id' => (string) $r->project_id,
                'project_name' => $r->project_name,
                'slug' => $r->slug,
                'commodity' => $r->commodity,
                'region' => $r->region,
                'collar_count' => (int) $r->collar_count,
                'curve_count' => (int) $r->curve_count,
                'passage_count' => $passages,
                'embedded_count' => $embedded,
                'embedding_pct' => $pct,
            ];
        }, $rows);
    }
}
