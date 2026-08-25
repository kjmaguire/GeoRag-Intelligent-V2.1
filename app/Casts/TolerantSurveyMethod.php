<?php

declare(strict_types=1);

namespace App\Casts;

use App\Enums\SurveyMethod;
use Illuminate\Contracts\Database\Eloquent\CastsAttributes;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Support\Facades\Log;

/**
 * `survey_method` as a SurveyMethod when it is one, and null when it is not.
 *
 * WHY THIS EXISTS
 *     Casting the column straight to the enum makes every read of a row
 *     outside the vocabulary throw
 *
 *         ValueError: "unknown" is not a valid backing value for enum
 *         App\Enums\SurveyMethod
 *
 *     and CollarController::show catches Throwable, so the whole collar
 *     detail endpoint answers 500 — for every collar in the project, not
 *     just the surveyed one.
 *
 *     That is not hypothetical. `_SURVEY_METHOD_DEFAULT = "unknown"` in
 *     ingest_tabular.py is written for every survey station whose sheet did
 *     not name an instrument, which is most of them, and the Discover-trace
 *     path adds `desurveyed_trace`. A single ingested survey file was enough
 *     to take the endpoint down.
 *
 * WHY THE ENUM IS NOT SIMPLY EXTENDED
 *     §04e calls this a closed vocabulary of instrument FAMILIES — Gyro,
 *     Magnetic, Multishot — and CLAUDE.md rule 6 reserves changes to it for
 *     the SME. It is also the wrong fix on the merits: `unknown` and
 *     `desurveyed_trace` are not instruments. They describe where the record
 *     came from, which is a different fact that the column currently has
 *     nowhere to put. Widening an instrument vocabulary to hold provenance
 *     would make the column mean two things and be much harder to undo than
 *     this cast.
 *
 *     So the vocabulary is untouched and the READ is made total. An
 *     out-of-vocabulary value is not an instrument we recognise, and null
 *     says exactly that. Callers that want the stored string can still read
 *     `getRawOriginal('survey_method')`, which is what CollarResource does
 *     so the API keeps returning a string and loses nothing.
 *
 * @implements CastsAttributes<SurveyMethod|null, SurveyMethod|string|null>
 */
class TolerantSurveyMethod implements CastsAttributes
{
    /**
     * @param array<string, mixed> $attributes
     */
    public function get(Model $model, string $key, mixed $value, array $attributes): ?SurveyMethod
    {
        if ($value === null || $value === '') {
            return null;
        }

        $method = SurveyMethod::tryFrom((string) $value);

        if ($method === null) {
            // Debug, not warning: on a project ingested from sheets that name
            // no instrument this is the common case, and at warning level it
            // would emit one line per survey row per request.
            Log::debug('survey_method outside the §04e vocabulary', [
                'value' => (string) $value,
                'model' => $model::class,
            ]);
        }

        return $method;
    }

    /**
     * Writes still go through the vocabulary.
     *
     * Nothing in the app writes this column today — the ingestion does, in
     * Python, straight to Postgres. If Eloquent ever starts writing it, an
     * out-of-vocabulary value should be a loud failure at the point of the
     * mistake rather than another silent degradation.
     *
     * @param array<string, mixed> $attributes
     */
    public function set(Model $model, string $key, mixed $value, array $attributes): ?string
    {
        if ($value === null) {
            return null;
        }

        if ($value instanceof SurveyMethod) {
            return $value->value;
        }

        return SurveyMethod::from((string) $value)->value;
    }
}
