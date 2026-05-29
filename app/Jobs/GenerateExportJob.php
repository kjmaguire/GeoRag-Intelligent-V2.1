<?php

declare(strict_types=1);

namespace App\Jobs;

use App\Models\Export;
use App\Services\Exports\CsaBundleExporter;
use App\Services\Exports\CsvAssaysExporter;
use App\Services\Exports\CsvCollarExporter;
use App\Services\Exports\CsvGeochemistryExporter;
use App\Services\Exports\CsvLithologyExporter;
use App\Services\Exports\CsvSamplesExporter;
use App\Services\Exports\DxfExporter;
use App\Services\Exports\GeoPackageExporter;
use App\Services\Exports\LasBundleExporter;
use App\Services\Exports\ShapefileExporter;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;

/**
 * Generates a data export, uploads the resulting file(s) to MinIO, generates a
 * 24-hour presigned download URL, and updates the exports record accordingly.
 *
 * Runs on the default Horizon queue. Dispatched by ExportController::store().
 * Octane-safe: no static state, connections released after each job.
 */
class GenerateExportJob implements ShouldQueue
{
    use Dispatchable;
    use InteractsWithQueue;
    use Queueable;
    use SerializesModels;

    /**
     * Maximum seconds before Horizon kills the job.
     * Large projects with many collars / LAS curves may generate sizeable files.
     */
    public int $timeout = 300;

    /**
     * No retries — an export is idempotent to re-create, but the user should
     * explicitly re-request rather than have silent duplicate uploads.
     */
    public int $tries = 1;

    public function __construct(
        private readonly string $exportId,
    ) {}

    public function handle(): void
    {
        /** @var Export|null $export */
        $export = Export::find($this->exportId);

        if (! $export) {
            Log::warning('GenerateExportJob: export record not found', [
                'export_id' => $this->exportId,
            ]);

            return;
        }

        Log::info('GenerateExportJob: starting', [
            'export_id' => $this->exportId,
            'export_type' => $export->export_type,
            'project_id' => $export->project_id,
        ]);

        $export->update(['status' => 'running']);

        try {
            $result = $this->generate($export);

            $localPath = $result['path'];
            $fileSize = $result['size'];
            // Bucket-scoped object key — no `georag-exports/` prefix because the
            // `s3-exports` disk is already bound to the MINIO_BUCKET_EXPORTS bucket.
            $minioKey = "{$export->export_id}/".basename($localPath);

            // Upload to MinIO via the dedicated exports disk (separate bucket
            // from the bronze layer so generated artifacts never pollute the
            // immutable raw archive).
            Storage::disk('s3-exports')->put($minioKey, fopen($localPath, 'r'));

            @unlink($localPath);

            // Generate a presigned URL valid for 24 hours.
            $expiresAt = now()->addHours(24);
            $signedUrl = Storage::disk('s3-exports')->temporaryUrl($minioKey, $expiresAt);

            $export->update([
                'status' => 'completed',
                'minio_path' => $minioKey,
                'download_url' => $signedUrl,
                'download_url_expires_at' => $expiresAt,
                'file_count' => 1,
                'total_size_bytes' => $fileSize,
                'completed_at' => now(),
            ]);

            Log::info('GenerateExportJob: completed', [
                'export_id' => $this->exportId,
                'minio_path' => $minioKey,
                'total_size_bytes' => $fileSize,
            ]);
        } catch (\Throwable $e) {
            Log::error('GenerateExportJob: failed', [
                'export_id' => $this->exportId,
                'exception' => $e->getMessage(),
                'trace' => $e->getTraceAsString(),
            ]);

            $export->update([
                'status' => 'failed',
                'error_message' => $e->getMessage(),
            ]);

            throw $e;
        }
    }

    /**
     * Dispatch to the correct generator based on export_type.
     *
     * @return array{path: string, size: int}
     */
    private function generate(Export $export): array
    {
        $filters = $export->filters ?? [];

        return match ($export->export_type) {
            'csv_collars' => (new CsvCollarExporter)->export($export->project_id, $filters),
            'csv_samples' => (new CsvSamplesExporter)->export($export->project_id, $filters),
            'csv_assays' => (new CsvAssaysExporter)->export($export->project_id, $filters),
            'csv_lithology' => (new CsvLithologyExporter)->export($export->project_id, $filters),
            'csv_geochem' => (new CsvGeochemistryExporter)->export($export->project_id, $filters),
            'csa_bundle' => (new CsaBundleExporter)->export($export->project_id, $filters),
            'shapefile' => (new ShapefileExporter)->export($export->project_id, $filters),
            'geopackage' => (new GeoPackageExporter)->export($export->project_id, $filters),
            'dxf' => (new DxfExporter)->export($export->project_id, $filters),
            'las_bundle' => (new LasBundleExporter)->export($export->project_id, $filters),
            default => throw new \InvalidArgumentException(
                "Unknown export_type: {$export->export_type}",
            ),
        };
    }
}
