<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\ProjectAggregates;

use App\Services\Citations\Resolvers\AbstractCitationResolver;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Resolves the citations FastAPI's response_assembler emits for a project
 * level tool result: `<prefix>project=<uuid>:<key>=<value>:...`.
 *
 * These cite a whole rowset (a project summary, its coverage, its drill
 * traces, its structures), not one record, so there is no row to fetch.
 * What can be checked is the project: it must be one the request may read.
 * The counts are what the tool returned when the answer was written; they
 * come from the id and are shown as that, never as a fresh measurement.
 *
 * None of these prefixes had a resolver, so every such chip opened the
 * Evidence Inspector on "Source type not recognized" (audit 2026-10-10
 * CH-4).
 */
abstract class AbstractProjectAggregateResolver extends AbstractCitationResolver
{
    /**
     * The `source_type` the Evidence Inspector receives.
     */
    abstract protected function sourceType(): string;

    /**
     * The inspector payload for a project the request may read.
     *
     * @param array<string, string> $fields
     *
     * @return array{title: string, text: string, metadata: array<string, mixed>}
     */
    abstract protected function describe(object $project, array $fields): array;

    /**
     * @param list<string>|null $projectIds
     */
    public function resolve(string $sourceId, ?string $workspaceId = null, ?array $projectIds = null): JsonResponse
    {
        if ($workspaceId === null || $projectIds === null || $projectIds === []) {
            return $this->notFound($sourceId);
        }

        $fields = $this->fields($sourceId);
        $project = $this->project($fields, $workspaceId, $projectIds);

        if ($project === null) {
            return $this->notFound($sourceId);
        }

        return response()->json([
            'source_type' => $this->sourceType(),
            'source_chunk_id' => $sourceId,
            ...$this->describe($project, $fields),
        ]);
    }

    /**
     * The `key=value` pairs after the prefix. The first occurrence of a key
     * wins.
     *
     * @return array<string, string>
     */
    protected function fields(string $sourceId): array
    {
        $fields = [];

        foreach (explode(':', substr($sourceId, strlen(static::prefix()))) as $part) {
            $pair = explode('=', $part, 2);

            if (count($pair) === 2 && $pair[0] !== '' && ! array_key_exists($pair[0], $fields)) {
                $fields[$pair[0]] = $pair[1];
            }
        }

        return $fields;
    }

    /**
     * The cited project, when the request may read it. `project=` must be a
     * full UUID, so a malformed id never reaches the uuid column.
     *
     * @param array<string, string> $fields
     * @param list<string> $projectIds
     */
    protected function project(array $fields, string $workspaceId, array $projectIds): ?object
    {
        $projectId = strtolower($fields['project'] ?? '');

        if (! Str::isUuid($projectId) || ! in_array($projectId, array_map('strtolower', $projectIds), true)) {
            return null;
        }

        return DB::table('silver.projects')
            ->where('project_id', $projectId)
            ->where('workspace_id', $workspaceId)
            ->first(['project_id', 'project_name']);
    }

    /**
     * A non-negative count from the id, or null when the field is absent or
     * not a number.
     *
     * @param array<string, string> $fields
     */
    protected function count(array $fields, string $key): ?int
    {
        $value = $fields[$key] ?? null;

        return $value !== null && ctype_digit($value) ? (int) $value : null;
    }

    /**
     * Structured 404, identical for "missing" and "another tenant's" so the
     * endpoint is not an existence oracle.
     */
    protected function notFound(string $sourceId): JsonResponse
    {
        return response()->json([
            'source_type' => $this->sourceType(),
            'source_chunk_id' => $sourceId,
            'text' => 'Project data not found',
        ], 404);
    }
}
