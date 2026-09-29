<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Enums\CollarStatus;
use App\Enums\HoleType;
use App\Support\HoleId;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

class StoreCollarRequest extends FormRequest
{
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        // $projectId comes from the route parameter {project}
        $projectId = $this->route('project');

        return [
            'hole_id' => [
                'required',
                'string',
                'max:50',
                // One collar per (project, canonical hole id) — §04e,
                // SME-approved 2026-09-29: "LEB-23-001" and "leb23001" are
                // the same hole, so the second is refused here instead of
                // failing on the unique index as a 500.
                function ($attribute, $value, $fail) use ($projectId) {
                    $canonical = HoleId::canonicalize(is_string($value) ? $value : null);
                    $exists = \DB::table('silver.collars')
                        ->where('project_id', $projectId)
                        ->where(function ($q) use ($value, $canonical): void {
                            $q->where('hole_id', $value);
                            if ($canonical !== null) {
                                $q->orWhere('hole_id_canonical', $canonical);
                            }
                        })
                        ->exists();
                    if ($exists) {
                        $fail('This hole ID already exists in the project.');
                    }
                },
            ],
            'easting' => ['required', 'numeric'],
            'northing' => ['required', 'numeric'],
            'elevation' => ['nullable', 'numeric'],
            // §04e (SME-approved, Kyle, 2026-09-29): optional, NULL when not
            // known — never 0 — and strictly positive when given.
            'total_depth' => ['nullable', 'numeric', 'gt:0'],
            // Validates against HoleType enum — single source of truth shared
            // with the Collar model cast and CollarFactory. Closes the
            // historical drift where the factory generated 'Auger' but this
            // validator rejected it (resolved 2026-05-07).
            'hole_type' => ['required', Rule::enum(HoleType::class)],
            'azimuth' => ['nullable', 'numeric', 'between:0,360'],
            // Dip from horizontal, negative = down. A positive dip is an
            // up-hole (§04e, 2026-09-29), matching chk_dip_range (-90..90).
            'dip' => ['nullable', 'numeric', 'between:-90,90'],
            'drill_date' => ['nullable', 'date'],
            'status' => ['nullable', Rule::enum(CollarStatus::class)],
        ];
    }

    public function messages(): array
    {
        // Build the user-facing list of allowed values from the enum itself
        // so this message stays in sync if a new HoleType case is added.
        $holeTypes = implode(', ', array_map(
            fn (HoleType $c): string => $c->value,
            HoleType::cases(),
        ));
        $collarStatuses = implode(', ', array_map(
            fn (CollarStatus $c): string => $c->value,
            CollarStatus::cases(),
        ));

        return [
            'hole_id.required' => 'A hole ID is required.',
            'hole_id.unique' => 'This hole ID already exists in the project.',
            'total_depth.gt' => 'Total depth must be greater than 0 (leave it empty when unknown).',
            // `Rule::enum(...)` validates with rule name `enum`, not `in`.
            'hole_type.enum' => "Hole type must be one of: {$holeTypes}.",
            'azimuth.between' => 'Azimuth must be between 0 and 360 degrees.',
            'dip.between' => 'Dip must be between -90 and 90 degrees (negative = down, positive = up-hole).',
            'status.enum' => "Status must be one of: {$collarStatuses}.",
        ];
    }
}
