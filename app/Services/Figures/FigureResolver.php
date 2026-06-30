<?php

declare(strict_types=1);

namespace App\Services\Figures;

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Throwable;

/**
 * Reads the figure manifest persisted by the §04p ingest pipeline at
 * silver.reports.resource_estimate->figures (JSONB array) and mints
 * presigned download URLs against the s3-bronze disk.
 *
 * Figure manifest shape (one entry per extracted figure):
 *
 *   {
 *     "idx":         0,
 *     "page":        12,
 *     "bbox":        [l, t, r, b],
 *     "caption":     "Figure 1: Cross-section A-A'",
 *     "pending_key": "figures/_pending/<sha256>/figure_0000_page_12.png",
 *     "key":         "figures/<report_id>/figure_0000_page_12.png",
 *     "bucket":      "bronze",
 *     "sha256":      "<png sha256>"
 *   }
 *
 * Persist (the Hatchet task running after ingest_pdf.parse) copies the
 * pending PNG to the canonical key under figures/{report_id}/. The
 * resolver only reads the manifest entries that have the canonical
 * ``key`` set — pending entries are skipped (the persist task hasn't
 * caught up yet).
 *
 * Octane-safe: no instance state; all I/O per call.
 */
final class FigureResolver
{
    /** Default presign TTL in seconds (1 hour — matches MinIO STS expiry sanity). */
    private const DEFAULT_TTL_SECONDS = 3600;

    /**
     * Return the figure manifest for a report with presigned PNG URLs.
     *
     * @param string $reportId UUID of the silver.reports row
     * @param int $ttlSeconds presigned URL lifetime
     *
     * @return list<array{
     *     idx:int,
     *     page:?int,
     *     bbox:?array,
     *     caption:string,
     *     key:string,
     *     sha256:?string,
     *     url:string,
     *     expires_at:string
     * }>
     */
    public function manifestFor(string $reportId, int $ttlSeconds = self::DEFAULT_TTL_SECONDS): array
    {
        $row = DB::connection('pgsql')
            ->table('silver.reports')
            ->where('report_id', $reportId)
            ->value('resource_estimate');

        if ($row === null) {
            return [];
        }

        $payload = is_array($row) ? $row : json_decode((string) $row, true);
        if (! is_array($payload)) {
            return [];
        }

        $figures = $payload['figures'] ?? null;
        if (! is_array($figures) || $figures === []) {
            return [];
        }

        $disk = Storage::disk('s3-bronze');
        $expires = now()->addSeconds($ttlSeconds);

        $out = [];
        foreach ($figures as $f) {
            $key = $f['key'] ?? null;
            if (! is_string($key) || $key === '') {
                // Pending → persist hasn't promoted it yet. Skip.
                continue;
            }

            try {
                $url = $disk->temporaryUrl($key, $expires);
            } catch (Throwable $e) {
                // Don't fail the whole manifest on one bad entry.
                continue;
            }

            $out[] = [
                'idx' => (int) ($f['idx'] ?? 0),
                'page' => isset($f['page']) ? (int) $f['page'] : null,
                'bbox' => is_array($f['bbox'] ?? null) ? $f['bbox'] : null,
                'caption' => (string) ($f['caption'] ?? ''),
                'key' => $key,
                'sha256' => isset($f['sha256']) ? (string) $f['sha256'] : null,
                'url' => $url,
                'expires_at' => $expires->toIso8601String(),
            ];
        }

        return $out;
    }
}
