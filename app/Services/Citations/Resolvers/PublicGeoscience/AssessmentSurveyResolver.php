<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\PublicGeoscience;

final class AssessmentSurveyResolver extends AbstractPgeoResolver
{
    public static function prefix(): string
    {
        return 'pg_assessment_survey:';
    }

    protected function tableName(): string
    {
        return 'public_geo.pg_assessment_survey';
    }

    protected function columns(): array
    {
        return [
            'id', 'jurisdiction_code', 'source_id', 'source_feature_id',
            'survey_type', 'source_url', 'last_seen_at',
        ];
    }

    protected function mergePayload(array $envelope, ?object $entity, array $parts): array
    {
        $typeLabel = match ($entity?->survey_type) {
            'airborne'    => 'Airborne survey',
            'ground'      => 'Ground survey',
            'underground' => 'Underground survey',
            default       => 'Assessment survey',
        };

        $envelope['title'] = "{$typeLabel} footprint";
        $envelope['text']  = sprintf(
            '%s footprint from the SMAD index. Detailed survey content lives in the linked assessment filing.',
            $typeLabel,
        );
        $envelope['entity'] = $entity ? [
            'id'                => $entity->id,
            'survey_type'       => $entity->survey_type,
            'source_url'        => $entity->source_url,
            'source_feature_id' => $entity->source_feature_id,
            'last_seen_at'      => $entity->last_seen_at,
        ] : null;

        return $envelope;
    }
}
