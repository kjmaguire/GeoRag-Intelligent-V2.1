<?php

declare(strict_types=1);

namespace App\Casts;

use BackedEnum;
use Illuminate\Contracts\Database\Eloquent\CastsAttributes;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Support\Facades\Log;
use InvalidArgumentException;

/**
 * A string column as a backed enum when its value is in the enum's vocabulary,
 * and null when it is not.
 *
 * Wired as `TolerantEnum::class.':'.SomeEnum::class` in a model's casts, e.g.
 * Collar's `hole_type` and `status`. String-backed enums only, which is what
 * every column this is for holds.
 *
 * WHY THIS EXISTS
 *     Casting such a column straight to a backed enum makes every READ of an
 *     out-of-vocabulary row throw
 *
 *         ValueError: "DDH" is not a valid backing value for enum
 *         App\Enums\HoleType
 *
 *     and the ingestion writes whatever the source file said. silver.collars
 *     `hole_type` and `status` are free text capped at varchar(20) (see
 *     silver_row_guard.py): "DDH", "Core", "Closed" and "completed" all land
 *     there, and `unknown` when a cell was blank or too long. One such row
 *     answered 500 for every page of GET /projects/{p}/collars that contained
 *     it, and for GET .../collars/{id}, because the resource reads the
 *     attribute while serialising. Same bug, same fix as TolerantSurveyMethod.
 *
 * WHY THE ENUM IS NOT SIMPLY EXTENDED
 *     The vocabulary is §04e's and belongs to the SME (CLAUDE.md rule 6); what
 *     a file happened to say is not a reason to widen it. So the vocabulary is
 *     untouched and the READ is made total. Out of vocabulary means "not a
 *     value we recognise", which null says exactly. Anything that must show or
 *     export what was stored reads `getRawOriginal()` — CollarResource and the
 *     collar exporters do — so the API and the files keep the geologist's own
 *     word instead of a blank.
 *
 * Writes still go through the vocabulary. StoreCollarRequest validates with
 * Rule::enum and this cast refuses anything the request would have refused, so
 * nothing the application itself writes can be outside it; only the ingestion
 * (which does not pass through Eloquent) can.
 *
 * @implements CastsAttributes<BackedEnum|null, BackedEnum|string|null>
 */
final class TolerantEnum implements CastsAttributes
{
    /** @var class-string<BackedEnum> */
    private readonly string $enum;

    /**
     * @param class-string<BackedEnum> $enum
     */
    public function __construct(string $enum)
    {
        if (! is_a($enum, BackedEnum::class, true)) {
            throw new InvalidArgumentException("TolerantEnum needs a backed enum, got [{$enum}].");
        }

        $this->enum = $enum;
    }

    /**
     * @param array<string, mixed> $attributes
     */
    public function get(Model $model, string $key, mixed $value, array $attributes): ?BackedEnum
    {
        if ($value === null || $value === '') {
            return null;
        }

        $case = $this->enum::tryFrom((string) $value);

        if ($case === null) {
            // Debug, not warning: on a project ingested from files that name
            // their own drilling methods this is the common case, and at
            // warning level it would be a line per collar per request.
            Log::debug('enum column holds a value outside its vocabulary', [
                'enum' => $this->enum,
                'attribute' => $key,
                'value' => (string) $value,
                'model' => $model::class,
            ]);
        }

        return $case;
    }

    /**
     * @param array<string, mixed> $attributes
     *
     * @throws \ValueError when the value is outside the enum's vocabulary
     */
    public function set(Model $model, string $key, mixed $value, array $attributes): string|int|null
    {
        if ($value === null) {
            return null;
        }

        if ($value instanceof $this->enum) {
            return $value->value;
        }

        return $this->enum::from($value)->value;
    }
}
