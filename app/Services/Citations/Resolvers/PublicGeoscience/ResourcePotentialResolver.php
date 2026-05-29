<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

final class ResourcePotentialResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_resource_potential_zone:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_resource_potential_zone';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'commodity', 'commodity_grouping', 'potential_rank',
            'methodology_ref', 'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $commodity = $entity?->commodity ? ucfirst((string) $entity->commodity) : 'Commodity unknown';
        $rank      = $entity?->potential_rank;

        $envelope['title'] = $rank !== null
            ? "{$commodity} Resource Potential — rank {$rank}/6"
            : "{$commodity} Resource Potential";
        $envelope['text'] = sprintf(
            '%s resource potential zone. Rank: %s. Methodology: %s.',
            $commodity,
            $rank !== null ? "{$rank}/6" : 'unspecified',
            $entity->methodology_ref ?? 'not referenced',
        );
        $envelope['entity'] = $entity ? [
            'id'                 => $entity->id,
            'commodity'          => $entity->commodity,
            'commodity_grouping' => $entity->commodity_grouping,
            'potential_rank'     => $entity->potential_rank,
            'methodology_ref'    => $entity->methodology_ref,
            'source_feature_id'  => $entity->source_feature_id,
            'last_seen_at'       => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
