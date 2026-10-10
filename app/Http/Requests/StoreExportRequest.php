<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Enums\CollarStatus;
use App\Enums\HoleType;
use BackedEnum;
use Closure;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Validator;

class StoreExportRequest extends FormRequest
{
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        return [
            'export_type' => [
                'required',
                'string',
                // Kept in lockstep with App\Jobs\GenerateExportJob::generate().
                'in:csv_collars,csv_samples,csv_assays,csv_lithology,csv_geochem,csa_bundle,shapefile,geopackage,dxf,las_bundle',
            ],

            // Optional filter bag — all keys are optional. The set is the
            // union of every exporter's supported filters; each exporter
            // ignores filters it doesn't understand. Validation rules
            // here are presence/type/range only, not "this filter only
            // applies to that export_type" — that level of conditional
            // validation isn't worth the form-request complexity.
            'filters' => ['nullable', 'array'],
            'filters.hole_id' => ['nullable', 'string', 'max:64'],
            // The collar vocabularies, read off the enums so they cannot drift
            // from them again. These two were hand-copied lists that had
            // already lost Auger, exploration, unknown, "In Progress" and
            // "Planned". Any case is accepted because the exporters compare
            // case-insensitively (CollarExportQuery::applyFilters): the
            // ingestion writes 'active' where the enum's first case is 'Active'.
            'filters.hole_type' => ['nullable', 'string', self::inVocabulary(HoleType::class)],
            'filters.status' => ['nullable', 'string', self::inVocabulary(CollarStatus::class)],
            'filters.drill_date_from' => ['nullable', 'date'],
            'filters.drill_date_to' => ['nullable', 'date'],
            'filters.min_depth' => ['nullable', 'numeric', 'min:0'],
            'filters.max_depth' => ['nullable', 'numeric', 'min:0'],

            // csv_samples filters
            'filters.from_depth_min' => ['nullable', 'numeric', 'min:0'],
            'filters.from_depth_max' => ['nullable', 'numeric', 'min:0'],
            'filters.sample_type' => ['nullable', 'string', 'max:32'],

            // csv_assays filters
            'filters.element' => ['nullable', 'string', 'max:8'],
            'filters.exclude_rejected' => ['nullable', 'boolean'],
            'filters.include_below_detection' => ['nullable', 'boolean'],

            // csv_lithology filters
            'filters.min_confidence' => ['nullable', 'numeric', 'between:0,1'],

            // csv_geochem filters
            'filters.include_ree' => ['nullable', 'boolean'],

            // CC-01 Item 6 — review-status filter. silver.* tables are the
            // "accepted" lane by design (rows only land after Silver Review
            // Queue commit). Default behaviour (omitted or 'accepted') is
            // unchanged — silver only. 'include_pending' unions with
            // review_queue.payload rows still in 'pending'/'in_review'.
            // 'pending_only' emits ONLY queued rows — useful for QA review.
            'filters.review_status' => ['nullable', 'string', 'in:accepted,include_pending,pending_only'],
        ];
    }

    /**
     * A rule that accepts any value of a backed enum, in any letter case.
     *
     * @param class-string<BackedEnum> $enum
     */
    private static function inVocabulary(string $enum): Closure
    {
        $values = array_map(static fn (BackedEnum $case): string => (string) $case->value, $enum::cases());
        $allowed = array_map('mb_strtolower', $values);

        return static function (string $attribute, mixed $value, Closure $fail) use ($values, $allowed): void {
            if (! is_string($value) || ! in_array(mb_strtolower($value), $allowed, true)) {
                $fail("The {$attribute} field must be one of: ".implode(', ', $values).' (any case).');
            }
        };
    }

    /**
     * Range checks that only apply when BOTH ends are supplied.
     *
     * LAR-9 (2026-09-29): these were `gt:filters.min_depth` /
     * `after_or_equal:filters.drill_date_from` rules, and Laravel fails a
     * field-reference comparison when the referenced field is absent — so
     * `{"filters": {"max_depth": 500}}` was rejected with "must be greater
     * than filters.min_depth". An open-ended range is a normal filter.
     *
     * @return array<int, Closure(Validator): void>
     */
    public function after(): array
    {
        return [
            function (Validator $validator): void {
                if ($validator->errors()->isNotEmpty()) {
                    return;
                }

                $filters = (array) $this->input('filters', []);

                foreach ([['min_depth', 'max_depth'], ['from_depth_min', 'from_depth_max']] as [$low, $high]) {
                    if (isset($filters[$low], $filters[$high]) && (float) $filters[$high] <= (float) $filters[$low]) {
                        $validator->errors()->add("filters.{$high}", "The filters.{$high} field must be greater than filters.{$low}.");
                    }
                }

                if (isset($filters['drill_date_from'], $filters['drill_date_to'])
                    && strtotime((string) $filters['drill_date_to']) < strtotime((string) $filters['drill_date_from'])) {
                    $validator->errors()->add('filters.drill_date_to', 'The filters.drill_date_to field must be a date after or equal to filters.drill_date_from.');
                }
            },
        ];
    }

    public function messages(): array
    {
        return [
            'export_type.in' => 'export_type must be one of: csv_collars, csv_samples, csv_assays, csv_lithology, csv_geochem, csa_bundle, shapefile, geopackage, dxf, las_bundle.',
            'filters.review_status.in' => 'filters.review_status must be one of: accepted (default), include_pending, pending_only.',
        ];
    }
}
