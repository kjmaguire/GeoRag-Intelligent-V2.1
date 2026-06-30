<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Enums\CollarStatus;
use App\Enums\HoleType;
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
                function ($attribute, $value, $fail) use ($projectId) {
                    $exists = \DB::table('silver.collars')
                        ->where('project_id', $projectId)
                        ->where('hole_id', $value)
                        ->exists();
                    if ($exists) {
                        $fail('This hole ID already exists in the project.');
                    }
                },
            ],
            'easting' => ['required', 'numeric'],
            'northing' => ['required', 'numeric'],
            'elevation' => ['nullable', 'numeric'],
            'total_depth' => ['required', 'numeric', 'min:0.01'],
            // Validates against HoleType enum — single source of truth shared
            // with the Collar model cast and CollarFactory. Closes the
            // historical drift where the factory generated 'Auger' but this
            // validator rejected it (resolved 2026-05-07).
            'hole_type' => ['required', Rule::enum(HoleType::class)],
            'azimuth' => ['nullable', 'numeric', 'between:0,360'],
            'dip' => ['nullable', 'numeric', 'between:-90,0'],
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
            'total_depth.min' => 'Total depth must be greater than 0.',
            // `Rule::enum(...)` validates with rule name `enum`, not `in`.
            'hole_type.enum' => "Hole type must be one of: {$holeTypes}.",
            'azimuth.between' => 'Azimuth must be between 0 and 360 degrees.',
            'dip.between' => 'Dip must be between -90 and 0 degrees.',
            'status.enum' => "Status must be one of: {$collarStatuses}.",
        ];
    }
}
