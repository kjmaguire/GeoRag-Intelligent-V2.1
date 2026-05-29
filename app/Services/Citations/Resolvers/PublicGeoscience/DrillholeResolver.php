<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

final class DrillholeResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_drillhole_collar:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_drillhole_collar';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'drillhole_id', 'drillhole_name', 'company', 'project_name',
            'date_drilled', 'drill_type', 'commodity_of_interest',
            'total_length_m', 'collar_elevation_m', 'stratigraphic_depths',
            'core_availability', 'core_storage', 'disposition',
            'source_url', 'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $displayName = $entity->drillhole_name ?? $entity->drillhole_id ?? 'Unknown drillhole';
        $commodities = $this->parsePgArray($entity->commodity_of_interest ?? null);
        $strat       = $entity?->stratigraphic_depths
            ? json_decode((string) $entity->stratigraphic_depths, true)
            : null;

        $envelope['title'] = "Drillhole {$displayName}";
        $envelope['text']  = sprintf(
            'Drillhole %s (%s) at %s by %s, drilled %s. Total depth: %s m. Targets: %s. Core: %s.',
            $displayName,
            $entity->drill_type ?? 'type unknown',
            $entity->project_name ?? 'unspecified project',
            $entity->company ?? 'unknown operator',
            $entity->date_drilled ?? 'date unknown',
            $entity->total_length_m !== null ? number_format((float) $entity->total_length_m, 1) : '—',
            $commodities ? implode(', ', $commodities) : 'not listed',
            $entity->core_availability ?? 'unknown',
        );
        $envelope['entity'] = $entity ? [
            'id'                    => $entity->id,
            'drillhole_id'          => $entity->drillhole_id,
            'drillhole_name'        => $entity->drillhole_name,
            'company'               => $entity->company,
            'project_name'          => $entity->project_name,
            'date_drilled'          => $entity->date_drilled,
            'drill_type'            => $entity->drill_type,
            'commodity_of_interest' => $commodities,
            'total_length_m'        => $entity->total_length_m,
            'collar_elevation_m'    => $entity->collar_elevation_m,
            'stratigraphic_depths'  => $strat,
            'core_availability'     => $entity->core_availability,
            'core_storage'          => $entity->core_storage,
            'disposition'           => $entity->disposition,
            'source_url'            => $entity->source_url,
            'source_feature_id'     => $entity->source_feature_id,
            'last_seen_at'          => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
