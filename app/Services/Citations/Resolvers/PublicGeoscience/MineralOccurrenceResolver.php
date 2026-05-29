<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

/**
 * Resolves `pg_mineral_occurrence:<source_id>:feature=<id>:pg_id=<uuid>`.
 *
 * Display label varies by jurisdiction:
 *   - CA-SK rows render as "SMDI <external_id>" (jurisdiction-native vocab)
 *   - Other jurisdictions use a generic "ID <external_id>" prefix
 *   (V1.2 schema rename — column is now `external_id`, was `smdi_id`).
 */
final class MineralOccurrenceResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_mineral_occurrence:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_mineral_occurrence';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'external_id', 'name', 'historic_names', 'status',
            'primary_commodities', 'associated_commodities',
            'commodity_grouping', 'discovery_type', 'production_flag',
            'reserves_resources', 'source_url', 'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $jurisdictionLabel = ($entity?->jurisdiction_code === 'CA-SK') ? 'SMDI' : 'ID';
        $displayName       = $entity->name
            ?? ($entity && $entity->external_id
                ? "{$jurisdictionLabel} {$entity->external_id}"
                : 'Unnamed occurrence');
        $primary = $this->parsePgArray($entity->primary_commodities ?? null);
        $assoc   = $this->parsePgArray($entity->associated_commodities ?? null);

        $envelope['title'] = ($entity?->external_id)
            ? "{$jurisdictionLabel} {$entity->external_id} · {$displayName}"
            : $displayName;
        $envelope['text'] = sprintf(
            '%s. Status: %s. Primary commodities: %s%s. Historical production: %s.',
            $displayName,
            $entity->status ?? 'unknown',
            $primary ? implode(', ', $primary) : 'not listed',
            $assoc ? '; associated ' . implode(', ', $assoc) : '',
            ($entity?->production_flag) ? 'yes' : 'none recorded',
        );
        $envelope['entity'] = $entity ? [
            'id'                     => $entity->id,
            'external_id'            => $entity->external_id,
            'name'                   => $entity->name,
            'historic_names'         => $this->parsePgArray($entity->historic_names),
            'status'                 => $entity->status,
            'primary_commodities'    => $primary,
            'associated_commodities' => $assoc,
            'commodity_grouping'     => $entity->commodity_grouping,
            'discovery_type'         => $entity->discovery_type,
            'production_flag'        => (bool) $entity->production_flag,
            'reserves_resources'     => $entity->reserves_resources,
            'source_url'             => $entity->source_url,
            'source_feature_id'      => $entity->source_feature_id,
            'last_seen_at'           => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
