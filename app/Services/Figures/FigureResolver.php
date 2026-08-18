<?php

declare(strict_types=1);

namespace App\Services\Figures;

use App\Services\StorageService;
use Illuminate\Support\Facades\DB;
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
 * Octane-safe: the injected StorageService is stateless (a pure disk-
 * selection façade, no per-request data); all I/O still happens per call.
 */
final class FigureResolver
{
    /** Default presign TTL in seconds (1 hour — matches MinIO STS expiry sanity). */
    private const DEFAULT_TTL_SECONDS = 3600;

    public function __construct(
        private readonly StorageService $storage,
    ) {}

    /**
     * Return the figure manifest for a report with presigned PNG URLs.
     *
     * DORMANT AS OF 2026-08-18 — this returns [] for every report in
     * production, and that is currently expected, not a bug in this class.
     * `silver.reports.resource_estimate->figures` is only written by
     * ingest_pdf.py's persist step when the parse result carries a non-empty
     * figure_manifest, and parse_pdf_report has returned figure_manifest=[]
     * unconditionally since docling (its only producer) was removed
     * 2026-07-29 — see that file's own comment, "This block stays wired up
     * for a future producer". Live check on the 7-document corpus: 7 reports
     * with a resource_estimate payload, 0 carrying a `figures` key. The
     * Reader's figure panel is therefore empty everywhere.
     *
     * WHEN A PRODUCER IS REVIVED, the two sides do not currently agree and
     * this method will silently return [] even with figures present:
     *   - ingest_pdf.py writes `resource_estimate["figures"]` as a DICT,
     *     `{"items": [...], "source": "figure_manifest_v1"}`, but the loop
     *     below iterates `$payload['figures']` expecting a flat LIST of
     *     figure entries. Iterating that dict yields the items array and the
     *     source string, neither of which is a figure entry.
     *   - the entries themselves record the object path as `minio_key`,
     *     while the loop below reads `$f['key']`.
     * Unwrap `['items']` and align the key name at that point; both are
     * left as-is here rather than guessed at, since there is no producer to
     * verify a change against.
     *
     * @param string $reportId UUID of the silver.reports row
     * @param int $ttlSeconds presigned URL lifetime
     *
     * @return list<array{
     *     idx:int,
     *     page:?int,
     *     bbox:?array<int, int|float>,
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

        $disk = $this->storage->bronzeReadOnly();
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
