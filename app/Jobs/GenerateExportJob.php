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
use App\Services\StorageService;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\MaxAttemptsExceededException;
use Illuminate\Queue\SerializesModels;
use Illuminate\Queue\TimeoutExceededException;
use Illuminate\Support\Facades\Log;

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

    /** User-facing reason stored when generation throws; never the raw exception text. */
    public const GENERIC_FAILURE_MESSAGE = 'Export failed unexpectedly. Please request it again.';

    public function __construct(
        private readonly string $exportId,
    ) {}

    public function handle(StorageService $storage): void
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
            // immutable raw archive). putOrFail, because a refused write used
            // to leave this export 'completed' with a link to nothing.
            $handle = fopen($localPath, 'r');
            if ($handle === false) {
                throw new \RuntimeException('Unable to open the generated export for upload.');
            }
            try {
                $storage->putOrFail($storage->exports(), $minioKey, $handle);
            } finally {
                if (is_resource($handle)) {
                    fclose($handle);
                }
                @unlink($localPath);
            }

            // Generate a presigned URL valid for 24 hours.
            $expiresAt = now()->addHours(24);
            $signedUrl = $storage->presignedUrl($storage->exports(), $minioKey, $expiresAt);

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

            // error_message is API-visible (ExportController returns the
            // model), and a raw exception message can carry SQL, file system
            // paths or an internal URL. The detail is in the log line above;
            // the row gets a neutral reason. failed() will not overwrite it.
            $export->update([
                'status' => 'failed',
                'error_message' => self::GENERIC_FAILURE_MESSAGE,
            ]);

            throw $e;
        }
    }

    /**
     * Terminal failure hook — the only code that runs when the worker, not
     * handle(), ends the job.
     *
     * LAR-6 (2026-09-29): handle()'s catch only sees exceptions thrown inside
     * the process. A Horizon timeout kills the worker (SIGALRM) and a
     * MaxAttemptsExceeded re-pop never enters handle() at all, so the row stayed
     * `running` forever: status polling never ended and `download` answered 409
     * indefinitely. This marks it failed with a reason a user can act on.
     *
     * Conditional on the row still being in flight: when handle()'s own catch
     * already wrote `failed` with the specific message and rethrew, the worker
     * calls this too, and that message must not be overwritten. A `completed`
     * row is never touched. The reason is generic by design; the exception
     * detail goes to the log, not to an API-visible column.
     */
    public function failed(?\Throwable $exception = null): void
    {
        $reason = match (true) {
            $exception instanceof TimeoutExceededException => sprintf(
                'Export timed out after %d seconds. Try a narrower filter, or request it again.',
                $this->timeout,
            ),
            $exception instanceof MaxAttemptsExceededException => 'Export was interrupted before it finished. Please request it again.',
            default => self::GENERIC_FAILURE_MESSAGE,
        };

        $updated = Export::query()
            ->whereKey($this->exportId)
            ->whereNotIn('status', ['completed', 'failed'])
            ->update([
                'status' => 'failed',
                'error_message' => $reason,
            ]);

        Log::error('GenerateExportJob: failed() hook', [
            'export_id' => $this->exportId,
            'marked_failed' => $updated > 0,
            'exception_class' => $exception !== null ? $exception::class : null,
            'exception' => $exception?->getMessage(),
        ]);
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
