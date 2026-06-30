<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

use App\Services\Citations\Resolvers\AbstractCitationResolver;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;

/**
 * Common scaffolding for every Public Geoscience (PGEO) citation resolver.
 *
 * All PGEO resolvers follow the same shape (plan §08):
 *   1. Parse the `pg_<canonical_type>:<source_id>:feature=<id>:pg_id=<uuid>`
 *      chunk id into its components.
 *   2. Look up the entity row by `pg_id` against a per-type table.
 *   3. Build the shared envelope (jurisdiction + source + license + staleness
 *      + cross-corpus references summary).
 *   4. Layer entity-specific `title`, `text`, and `entity` fields on top.
 *
 * Concrete subclasses implement four template methods:
 *   - `prefix()`            — `pg_<canonical_type>:`
 *   - `tableName()`         — `public_geo.pg_<table>`
 *   - `columns()`           — column list for the SELECT
 *   - `mergePayload()`      — merge `title`, `text`, `entity` onto the envelope
 *
 * Everything else (parse, envelope construction, references summary) is
 * inherited so adding a PGEO entity type is a ~30-line concrete subclass.
 */
abstract class AbstractPgeoResolver extends AbstractCitationResolver
{
    /**
     * Fully-qualified PostgreSQL table name (e.g. `public_geo.pg_mine`).
     */
    abstract protected function tableName(): string;

    /**
     * Columns to SELECT from the entity table.
     *
     * @return list<string>
     */
    abstract protected function columns(): array;

    /**
     * Layer entity-specific `title`, `text`, and `entity` fields on top of
     * the shared envelope. Concrete resolvers typically:
     *
     *     $envelope['title']  = "...";
     *     $envelope['text']   = sprintf(...);
     *     $envelope['entity'] = $entity ? [ ... ] : null;
     *     return $envelope;
     *
     * @param array<string, mixed> $envelope
     * @param array<string, ?string> $parts
     *
     * @return array<string, mixed>
     */
    abstract protected function mergePayload(array $envelope, ?object $entity, array $parts): array;

    public function resolve(string $sourceId): JsonResponse
    {
        $parts = $this->parseChunkId($sourceId);
        $entity = $this->loadEntity($parts['pg_id']);
        $envelope = $this->buildEnvelope($sourceId, $parts);
        $payload = $this->mergePayload($envelope, $entity, $parts);

        return response()->json($payload);
    }

    // ─────────────────────────────────────────────────────────────────────
    // Parsing
    // ─────────────────────────────────────────────────────────────────────

    /**
     * Parse `pg_<canonical_type>:<source_id>:feature=<id>:pg_id=<uuid>`.
     *
     * canonical_type sits between `pg_` and `:`; the source_id itself can
     * contain colons (e.g. `CA-SK-MINE-LOC`), so use targeted extraction
     * rather than a naive `split(':')`.
     *
     * Letters/digits/underscore in the type slot — supports future canonical
     * types like `pg_resource_potential_v2` without a code change.
     *
     * @return array{canonical_type: ?string, source_id: ?string, feature_id: ?string, pg_id: ?string}
     */
    protected function parseChunkId(string $sourceId): array
    {
        preg_match('/^pg_([a-z0-9_]+):([^:]+)/', $sourceId, $base);
        $canonicalType = $base[1] ?? null;
        $publicSourceId = $base[2] ?? null;

        preg_match('/feature=([^:]+)/', $sourceId, $f);
        $featureId = $f[1] ?? null;

        preg_match('/pg_id=([^:]+)/', $sourceId, $p);
        $pgId = $p[1] ?? null;

        return [
            'canonical_type' => $canonicalType,
            'source_id' => $publicSourceId,
            'feature_id' => $featureId,
            'pg_id' => $pgId,
        ];
    }

    // ─────────────────────────────────────────────────────────────────────
    // Entity lookup
    // ─────────────────────────────────────────────────────────────────────

    /**
     * Fetch the entity row from the per-type table. Returns null when the
     * `pg_id` is missing from the chunk_id (malformed citation) or when the
     * row does not exist (entity dropped from PG since the citation was
     * minted — common with refreshable PGEO sources).
     */
    protected function loadEntity(?string $pgId): ?object
    {
        if ($pgId === null) {
            return null;
        }

        return DB::table($this->tableName())
            ->where('id', $pgId)
            ->first($this->columns());
    }

    // ─────────────────────────────────────────────────────────────────────
    // Shared envelope (jurisdiction + source + license + staleness)
    // ─────────────────────────────────────────────────────────────────────

    /**
     * Build the shared response envelope every PGEO resolver returns.
     *
     * @param array{canonical_type: ?string, source_id: ?string, feature_id: ?string, pg_id: ?string} $parts
     *
     * @return array<string, mixed>
     */
    protected function buildEnvelope(string $sourceChunkId, array $parts): array
    {
        $sourceRow = DB::table('public_geo.sources as s')
            ->join('public_geo.jurisdictions as j', 'j.jurisdiction_code', '=', 's.jurisdiction_code')
            ->where('s.source_id', $parts['source_id'])
            ->select([
                's.source_id',
                's.name as source_name',
                's.canonical_type',
                's.service_url',
                's.license_summary',
                's.license_url',
                's.last_refreshed_at',
                'j.jurisdiction_code',
                'j.display_name as jurisdiction_name',
                'j.primary_authority',
            ])
            ->first();

        $lastRefreshedAt = $sourceRow?->last_refreshed_at;
        $stalenessSeconds = null;
        if ($lastRefreshedAt !== null) {
            try {
                // Carbon 3 changed diffIn*() default from unsigned to signed.
                // Without `absolute=true`, past timestamps return negative,
                // and the max(0,…) clamp would pin every PGEO citation to
                // staleness_seconds=0 — silently defeating the plan §08
                // "staleness age shown when serving cached data" contract.
                $stalenessSeconds = max(
                    0,
                    (int) now()->diffInSeconds($lastRefreshedAt, absolute: true),
                );
            } catch (\Throwable) {
                $stalenessSeconds = null;
            }
        }

        return [
            'source_type' => 'public_geo',
            'corpus' => 'public_geo',
            'canonical_type' => $parts['canonical_type'],
            'source_chunk_id' => $sourceChunkId,
            'jurisdiction' => [
                'code' => $sourceRow?->jurisdiction_code,
                'name' => $sourceRow?->jurisdiction_name,
                'authority' => $sourceRow?->primary_authority,
            ],
            'source' => [
                'source_id' => $parts['source_id'],
                'name' => $sourceRow?->source_name,
                'service_url' => $sourceRow?->service_url,
            ],
            'license' => [
                'summary' => $sourceRow?->license_summary,
                'url' => $sourceRow?->license_url,
            ],
            'refresh' => [
                'last_refreshed_at' => $lastRefreshedAt,
                'staleness_seconds' => $stalenessSeconds,
            ],
            'references_summary' => $this->loadEntityReferencesSummary(
                $parts['pg_id'],
                $parts['canonical_type'],
            ),
            'title' => null,
            'text' => null,
            'entity' => null,
            'metadata' => [
                'feature_id' => $parts['feature_id'],
                'pg_id' => $parts['pg_id'],
            ],
        ];
    }

    /**
     * Cross-corpus link summary — "Referenced in N reports" surface for the
     * citation card (plan §07d).
     *
     * @return array{count: int, documents: array<int, array<string, mixed>>}
     */
    protected function loadEntityReferencesSummary(?string $pgId, ?string $canonicalType): array
    {
        if ($pgId === null || $canonicalType === null) {
            return ['count' => 0, 'documents' => []];
        }

        $count = (int) DB::table('public_geo.document_entity_links')
            ->where('entity_id', $pgId)
            ->where('canonical_type', $canonicalType)
            ->whereNull('superseded_at')
            ->count();

        if ($count === 0) {
            return ['count' => 0, 'documents' => []];
        }

        $rows = DB::table('public_geo.document_entity_links as l')
            ->leftJoin('silver.reports as r', 'r.report_id', '=', 'l.document_id')
            ->where('l.entity_id', $pgId)
            ->where('l.canonical_type', $canonicalType)
            ->whereNull('l.superseded_at')
            ->orderByDesc('l.established_at')
            ->limit(5)
            ->get([
                'l.document_id',
                'l.document_filename',
                'l.confidence',
                'l.signals',
                'l.established_at',
                'l.established_by',
                'r.title',
                'r.filing_date',
            ]);

        return [
            'count' => $count,
            'documents' => $rows->map(fn ($row) => [
                'document_id' => $row->document_id,
                'title' => $row->title,
                'filename' => $row->document_filename,
                'filing_date' => $row->filing_date,
                'confidence' => (float) $row->confidence,
                'signals' => $this->decodeSignals($row->signals),
                'established_at' => $row->established_at,
                'established_by' => $row->established_by,
            ])->all(),
        ];
    }
}
