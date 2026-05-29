<?php

namespace App\Console\Commands\Ingestion;

use App\Services\FastApiJwtMinter;
use Illuminate\Console\Attributes\Description;
use Illuminate\Console\Attributes\Signature;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use RuntimeException;

#[Signature('ingest:reingest-project {projectId : silver.projects.project_id (uuid)} {--dry-run : list actions without modifying anything} {--missing-only : skip files already in silver.reports; do NOT delete existing rows or Qdrant points; useful when some workflows got cancelled by Hatchet concurrency} {--throttle-ms=2000 : sleep this long between trigger calls to avoid Hatchet GROUP_ROUND_ROBIN cancellations} {--qdrant-collection=georag_reports} {--qdrant-host=qdrant} {--qdrant-port=6333} {--actor-id=0}')]
#[Description('Re-ingest every PDF currently sitting in bronze (S3) under reports/{projectId}/. Deletes existing silver.reports rows for the project (cascades to document_passages) and the corresponding Qdrant points, then fires ingest_pdf Hatchet workflows for each S3 object so the new chunker reprocesses them end-to-end.')]
class ReingestProject extends Command
{
    public function handle(FastApiJwtMinter $jwtMinter): int
    {
        $projectId = (string) $this->argument('projectId');
        if (! Str::isUuid($projectId)) {
            $this->error("projectId must be a UUID, got: {$projectId}");

            return self::INVALID;
        }

        $dryRun = (bool) $this->option('dry-run');
        $missingOnly = (bool) $this->option('missing-only');
        $throttleMs = max(0, (int) $this->option('throttle-ms'));
        $collection = (string) $this->option('qdrant-collection');
        $qdrantHost = (string) $this->option('qdrant-host');
        $qdrantPort = (int) $this->option('qdrant-port');
        $actorId = (int) $this->option('actor-id');

        $project = DB::table('silver.projects')->where('project_id', $projectId)->first();
        if (! $project) {
            $this->error("Project {$projectId} not found in silver.projects.");

            return self::FAILURE;
        }
        $workspaceId = $project->workspace_id;
        $this->info("Project: {$project->slug} (workspace={$workspaceId})");

        $prefix = "reports/{$projectId}/";
        $disk = Storage::disk('s3');
        $keys = $disk->files($prefix);
        if (empty($keys)) {
            $this->warn("No PDFs found in s3://{$disk->getConfig()['bucket']}/{$prefix} — nothing to re-ingest.");

            return self::SUCCESS;
        }
        $this->info('Found '.count($keys).' S3 objects to re-ingest.');

        $existingReports = DB::table('silver.reports')
            ->where('project_id', $projectId)
            ->select('report_id', 'title', 'source_file_sha256')
            ->get();
        $this->info('Found '.$existingReports->count().' existing silver.reports row(s).');

        // In --missing-only mode we leave existing reports + their Qdrant points alone.
        // Need to hash each S3 object to know which are already ingested.
        $keysToTrigger = $keys;
        if ($missingOnly) {
            $haveShas = $existingReports->pluck('source_file_sha256')->filter()->all();
            $keysToTrigger = [];
            foreach ($keys as $k) {
                $sha = hash('sha256', $disk->get($k));
                if (in_array($sha, $haveShas, true)) {
                    $this->line("  skip (already ingested): {$k}");

                    continue;
                }
                $keysToTrigger[] = $k;
            }
            $this->info('--missing-only: '.count($keysToTrigger).' file(s) need ingestion.');
        }

        if ($dryRun) {
            $this->line('--dry-run set; here is what would happen:');
            if (! $missingOnly) {
                foreach ($existingReports as $r) {
                    $this->line("  DELETE silver.reports report_id={$r->report_id} title=\"{$r->title}\"");
                }
                $this->line("  DELETE Qdrant points where payload.project_id={$projectId} from collection={$collection}");
            }
            foreach ($keysToTrigger as $k) {
                $size = $disk->size($k);
                $this->line("  TRIGGER ingest_pdf minio_key={$k} size={$size}");
            }

            return self::SUCCESS;
        }

        if (! $missingOnly) {
            $this->info('Deleting Qdrant points...');
            $deleted = $this->deleteQdrantPoints($qdrantHost, $qdrantPort, $collection, $projectId);
            $this->line("  Qdrant: {$deleted}");

            $this->info('Deleting silver.reports (cascades to document_passages, ingest_extractions, etc.)...');
            $cnt = DB::table('silver.reports')->where('project_id', $projectId)->delete();
            $this->line("  silver.reports: {$cnt} row(s) removed");
        }

        $serviceKey = env('FASTAPI_SERVICE_KEY');
        if (! $serviceKey) {
            $this->error('FASTAPI_SERVICE_KEY not configured.');

            return self::FAILURE;
        }
        $triggerUrl = rtrim(
            config('services.fastapi.url') ?: env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        ).'/internal/v1/shadow/ingest_pdf/trigger';

        $jwt = $jwtMinter->mint(
            userId: $actorId,
            projectId: $projectId,
            roles: ['shadow:trigger'],
        );

        $triggered = 0;
        $failed = 0;
        foreach ($keysToTrigger as $i => $key) {
            $fileSize = $disk->size($key);
            $correlationToken = (string) Str::uuid();
            try {
                $resp = Http::withHeaders([
                    'Authorization' => "Bearer {$jwt}",
                    'X-Service-Key' => $serviceKey,
                ])->timeout(15)->post($triggerUrl, [
                    'workspace_id' => $workspaceId,
                    'project_id' => $projectId,
                    'minio_key' => $key,
                    'file_size' => $fileSize,
                    'vendor_profile_id' => null,
                    'correlation_token' => $correlationToken,
                    'actor_id' => $actorId ?: null,
                ]);
                if (! $resp->successful()) {
                    throw new RuntimeException("HTTP {$resp->status()}: ".substr($resp->body(), 0, 200));
                }
                $runId = $resp->json('workflow_run_id') ?: '?';
                $this->line("  triggered: {$key} run_id={$runId}");
                $triggered++;
            } catch (\Throwable $e) {
                $failed++;
                $this->error("  FAILED {$key}: ".$e->getMessage());
                Log::warning('reingest-project trigger failed', [
                    'project_id' => $projectId,
                    'minio_key' => $key,
                    'error' => $e->getMessage(),
                ]);
            }
            // Throttle between triggers so Hatchet's per-workspace concurrency
            // (max_runs=1, GROUP_ROUND_ROBIN) queues rather than cancels excess
            // runs. Without this every burst-fire above ~4 triggers loses the
            // tail to silent CANCELLED events at queue-depth saturation.
            if ($throttleMs > 0 && $i < count($keysToTrigger) - 1) {
                usleep($throttleMs * 1000);
            }
        }

        $this->info("Done. triggered={$triggered} failed={$failed}");
        $this->line('Workflows run asynchronously in Hatchet. Watch progress in the /ingestion-runs UI or:');
        $this->line('  docker logs -f georag-hatchet-worker-ingestion');

        return $failed === 0 ? self::SUCCESS : self::FAILURE;
    }

    /**
     * Delete every Qdrant point for the project. Returns a human-readable status.
     */
    private function deleteQdrantPoints(string $host, int $port, string $collection, string $projectId): string
    {
        $url = "http://{$host}:{$port}/collections/{$collection}/points/delete?wait=true";
        $resp = Http::timeout(60)->post($url, [
            'filter' => [
                'must' => [
                    ['key' => 'project_id', 'match' => ['value' => $projectId]],
                ],
            ],
        ]);
        if (! $resp->successful()) {
            return 'delete failed HTTP '.$resp->status().' '.substr($resp->body(), 0, 200);
        }

        return $resp->json('status', 'ok').' (operation_id='.$resp->json('result.operation_id', '?').')';
    }
}
