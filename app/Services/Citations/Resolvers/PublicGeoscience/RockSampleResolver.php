<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

final class RockSampleResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_rock_sample:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_rock_sample';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'station', 'sample_number', 'geologist', 'geographic_area',
            'report_number', 'map_number', 'map_scale',
            'nts_250k', 'nts_50k', 'date_collected',
            'source_url', 'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $displayName = $entity->sample_number
            ?? $entity->station
            ?? 'Rock sample (unlabelled)';

        $envelope['title'] = "{$displayName} — Rock Sample";
        $envelope['text']  = sprintf(
            "Government rock sample '%s' collected in %s (NTS %s). Geologist: %s. Report: %s.",
            $displayName,
            $entity->geographic_area ?? 'unspecified area',
            $entity->nts_250k ?? 'unknown tile',
            $entity->geologist ?? 'unrecorded',
            $entity->report_number ?? 'not linked',
        );
        $envelope['entity'] = $entity ? [
            'id'                => $entity->id,
            'sample_number'     => $entity->sample_number,
            'station'           => $entity->station,
            'geologist'         => $entity->geologist,
            'geographic_area'   => $entity->geographic_area,
            'report_number'     => $entity->report_number,
            'map_number'        => $entity->map_number,
            'map_scale'         => $entity->map_scale,
            'nts_250k'          => $entity->nts_250k,
            'nts_50k'           => $entity->nts_50k,
            'date_collected'    => $entity->date_collected,
            'source_url'        => $entity->source_url,
            'source_feature_id' => $entity->source_feature_id,
            'last_seen_at'      => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
