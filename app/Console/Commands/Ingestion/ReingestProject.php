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

#[Signature('ingest:reingest-project {projectId : silver.projects.project_id (uuid)} {--dry-run : list actions without modifying anything} {--missing-only : skip files already in silver.reports; do NOT delete existing rows or Qdrant points; useful when some workflows got cancelled by Hatchet concurrency} {--keys-file= : path to a JSON file containing an array of objects with a "minio_key" field; only those keys are re-triggered (intersected with what is actually in S3). Composes with --missing-only.} {--throttle-ms=2000 : sleep this long between trigger calls to avoid Hatchet GROUP_ROUND_ROBIN cancellations} {--qdrant-collection= : Qdrant collection to wipe on a destructive (non --missing-only) run. Defaults to the canonical collection per ADR-0010: georag_chunks when RETRIEVAL_USE_DOCUMENT_PASSAGES is true, else the legacy georag_reports.} {--qdrant-host=qdrant} {--qdrant-port=6333} {--actor-id=0}')]
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
        // --keys-file always runs in additive (missing-only) mode. Recovery
        // is by definition a "re-trigger this specific subset"; we never
        // want the destructive delete-all branch to fire when the operator
        // is hand-feeding a recovery manifest.
        if ($this->option('keys-file') && ! $missingOnly) {
            $this->warn('--keys-file: forcing --missing-only (recovery is always additive).');
            $missingOnly = true;
        }
        $throttleMs = max(0, (int) $this->option('throttle-ms'));
        // ADR-0010: canonical RAG collection is georag_chunks (document
        // passages). The legacy georag_reports collection is only
        // retained for the report-level path that predates the cutover.
        // Default tracks the same env flag FastAPI reads
        // (RETRIEVAL_USE_DOCUMENT_PASSAGES) so a destructive re-ingest
        // wipes whatever Qdrant collection retrieval is actually serving.
        $collectionOpt = $this->option('qdrant-collection');
        $collection = $collectionOpt !== null && $collectionOpt !== ''
            ? (string) $collectionOpt
            : ($this->canonicalQdrantCollection());
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

        // --keys-file: targeted recovery mode. Restrict $keys to only those
        // minio_keys present in the JSON file. Used to recover a specific
        // batch of files that got CANCELLED by Hatchet concurrency without
        // re-triggering every PDF under reports/{projectId}/.
        //
        // File shape: [{"minio_key":"reports/<uuid>/...pdf", ...}, ...]
        // Extra keys per entry are ignored.
        $keysFile = $this->option('keys-file');
        if ($keysFile) {
            try {
                $wanted = $this->loadWantedKeys($keysFile);
            } catch (\InvalidArgumentException $e) {
                $this->error($e->getMessage());

                return self::INVALID;
            }
            $wantedCount = count($wanted);
            $before = count($keys);
            $keys = array_values(array_filter(
                $keys,
                fn (string $k): bool => isset($wanted[$k]),
            ));
            $missingInS3 = $wantedCount - count($keys);
            $this->info(
                '--keys-file: '.count($keys).'/'.$wantedCount.' wanted keys found in S3'
                    .' (filtered from '.$before.' total S3 objects'
                    .($missingInS3 > 0 ? ", {$missingInS3} wanted keys NOT in S3" : '')
                    .').',
            );
            if (empty($keys)) {
                $this->warn('None of the requested keys are present in S3 — nothing to re-ingest.');

                return self::SUCCESS;
            }
        }

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
     * Resolve the default Qdrant collection per ADR-0010.
     *
     * The FastAPI side reads `RETRIEVAL_USE_DOCUMENT_PASSAGES` (defaults
     * to true in `src/fastapi/app/config.py`). When that flag is true,
     * retrieval serves `georag_chunks`; otherwise it serves the legacy
     * `georag_reports`. A destructive re-ingest should wipe whatever
     * collection retrieval actually reads from.
     *
     * Mirrors the FastAPI flag semantics: any of 1/true/on/yes (case
     * insensitive) counts as true. Unset → defaults to true (matches the
     * Pydantic Settings default).
     */
    protected function canonicalQdrantCollection(): string
    {
        $raw = env('RETRIEVAL_USE_DOCUMENT_PASSAGES');
        if ($raw === null || $raw === '') {
            return 'georag_chunks';
        }
        $useChunks = in_array(
            strtolower((string) $raw),
            ['1', 'true', 'on', 'yes'],
            true,
        );

        return $useChunks ? 'georag_chunks' : 'georag_reports';
    }

    /**
     * Parse a --keys-file manifest into a set of wanted minio_keys.
     *
     * The manifest is a JSON array of objects; each object must carry a
     * non-empty string `minio_key` field. Extra fields are ignored.
     * Duplicates collapse via the array-as-set return shape.
     *
     * @return array<string, true> map of minio_key => true (set semantics)
     *
     * @throws \InvalidArgumentException with an operator-readable reason
     */
    protected function loadWantedKeys(string $path): array
    {
        if (! is_file($path)) {
            throw new \InvalidArgumentException("--keys-file not found: {$path}");
        }
        $raw = file_get_contents($path);
        try {
            $entries = json_decode($raw, true, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException $e) {
            throw new \InvalidArgumentException(
                "--keys-file is not valid JSON: {$e->getMessage()}",
            );
        }
        // Require an actual JSON array (not an object). `array_is_list`
        // returns false on associative arrays like {"minio_key": "x"};
        // catching that explicitly produces a clearer operator message
        // than letting it fall through to "no minio_key entries".
        if (! is_array($entries) || ! array_is_list($entries)) {
            throw new \InvalidArgumentException(
                '--keys-file must contain a JSON array of objects.',
            );
        }
        /** @var array<string, true> $wanted */
        $wanted = [];
        foreach ($entries as $e) {
            if (is_array($e) && ! empty($e['minio_key']) && is_string($e['minio_key'])) {
                $wanted[$e['minio_key']] = true;
            }
        }
        if ($wanted === []) {
            throw new \InvalidArgumentException(
                '--keys-file contained no minio_key entries.',
            );
        }

        return $wanted;
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
