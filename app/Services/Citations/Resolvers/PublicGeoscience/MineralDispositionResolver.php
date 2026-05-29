<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

final class MineralDispositionResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_mineral_disposition:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_mineral_disposition';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'disposition_number', 'disposition_type', 'status',
            'holder_name', 'issue_date', 'expiry_date',
            'area_ha', 'commodity_codes', 'geographic_area',
            'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $typeLabel   = ucfirst(str_replace('_', ' ', $entity->disposition_type ?? 'mineral'));
        $statusLabel = ucfirst($entity->status ?? 'unknown');
        $number      = $entity->disposition_number ?? 'no number';
        $commodities = $this->parsePgArray($entity->commodity_codes ?? null);

        $envelope['title'] = "{$typeLabel} disposition {$number} ({$statusLabel})";
        $envelope['text']  = sprintf(
            '%s disposition %s held by %s. Status: %s. Area: %s ha. Issued: %s. Expires: %s.',
            $typeLabel,
            $number,
            $entity->holder_name ?? 'unrecorded holder',
            $statusLabel,
            $entity?->area_ha !== null ? number_format((float) $entity->area_ha, 2) : '—',
            $entity->issue_date ?? 'unknown',
            $entity->expiry_date ?? 'unknown',
        );
        $envelope['entity'] = $entity ? [
            'id'                 => $entity->id,
            'disposition_number' => $entity->disposition_number,
            'disposition_type'   => $entity->disposition_type,
            'status'             => $entity->status,
            'holder_name'        => $entity->holder_name,
            'issue_date'         => $entity->issue_date,
            'expiry_date'        => $entity->expiry_date,
            'area_ha'            => $entity->area_ha,
            'commodity_codes'    => $commodities,
            'geographic_area'    => $entity->geographic_area,
            'source_feature_id'  => $entity->source_feature_id,
            'last_seen_at'       => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
