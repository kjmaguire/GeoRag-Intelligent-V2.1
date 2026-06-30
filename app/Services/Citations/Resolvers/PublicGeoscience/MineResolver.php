<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

/**
 * Resolves `pg_mine:<source_id>:feature=<id>:pg_id=<uuid>` to a description
 * of a Public Geoscience mine record.
 */
final class MineResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_mine:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_mine';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'name', 'status', 'commodities', 'commodity_grouping',
            'operator', 'source_url', 'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $displayName = $entity->name ?? 'Unnamed mine';
        $commodities = $this->parsePgArray($entity->commodities ?? null);
        $statusLabel = $entity->status ?? 'status unknown';

        $envelope['title'] = "{$displayName} — Mine ("
            .ucfirst(str_replace('-', ' ', $statusLabel)).')';
        $envelope['text'] = sprintf(
            '%s: %s operated by %s. Commodities: %s.',
            $displayName,
            $statusLabel,
            $entity->operator ?? 'operator unspecified',
            $commodities ? implode(', ', $commodities) : 'not listed',
        );
        $envelope['entity'] = $entity ? [
            'id' => $entity->id,
            'name' => $entity->name,
            'status' => $entity->status,
            'commodities' => $commodities,
            'commodity_grouping' => $entity->commodity_grouping,
            'operator' => $entity->operator,
            'source_url' => $entity->source_url,
            'source_feature_id' => $entity->source_feature_id,
            'last_seen_at' => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
