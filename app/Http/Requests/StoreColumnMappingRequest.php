<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Models\ColumnMapping;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

class StoreColumnMappingRequest extends FormRequest
{
    /**
     * Authorization is handled at the route level via auth:sanctum middleware.
     */
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        // vendor_profile_id is bound from the route (nested resource URL), not
        // the request body. Do not expose it as a fillable request field.
        //
        // The two unique constraints mirror the DB schema:
        //   UNIQUE(vendor_profile_id, parser_type, canonical_field)
        //   UNIQUE(vendor_profile_id, parser_type, source_column)
        // We validate at the application layer so that duplicate attempts return
        // 422 from validation (testable in SQLite). The controller also catches
        // UniqueConstraintViolationException as a DB-level belt-and-suspenders
        // safety net and returns 409 on real Postgres.
        $vendorProfileId = $this->route('vendor_profile')?->id;
        $parserType = $this->input('parser_type');

        return [
            'parser_type' => ['required', Rule::in(ColumnMapping::PARSER_TYPES)],
            'canonical_field' => [
                'required',
                'string',
                'max:64',
                Rule::unique('column_mappings', 'canonical_field')
                    ->where('vendor_profile_id', $vendorProfileId)
                    ->where('parser_type', $parserType),
            ],
            'source_column' => [
                'required',
                'string',
                'max:255',
                Rule::unique('column_mappings', 'source_column')
                    ->where('vendor_profile_id', $vendorProfileId)
                    ->where('parser_type', $parserType),
            ],
            'source_unit' => ['nullable', 'string', 'max:32'],
            'target_unit' => ['nullable', 'string', 'max:32'],
            'notes' => ['nullable', 'string'],
        ];
    }

    public function messages(): array
    {
        return [
            'parser_type.in' => 'parser_type must be one of: '.implode(', ', ColumnMapping::PARSER_TYPES).'.',
            'canonical_field.unique' => 'A mapping for this (vendor_profile, parser_type, canonical_field) combination already exists.',
            'source_column.unique' => 'A mapping for this (vendor_profile, parser_type, source_column) combination already exists.',
        ];
    }
}
