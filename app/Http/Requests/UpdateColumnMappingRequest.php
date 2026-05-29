<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Models\ColumnMapping;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

class UpdateColumnMappingRequest extends FormRequest
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
        // parser_type is intentionally excluded from the update rules.
        // Changing parser_type post-creation is too likely to silently
        // cascade bugs in the ingestion pipeline (mappings are keyed on the
        // (profile, parser_type, canonical_field) tuple). If rekeying is
        // truly needed the caller should delete and recreate the mapping.
        return [
            'canonical_field' => ['sometimes', 'required', 'string', 'max:64'],
            'source_column'   => ['sometimes', 'required', 'string', 'max:255'],
            'source_unit'     => ['sometimes', 'nullable', 'string', 'max:32'],
            'target_unit'     => ['sometimes', 'nullable', 'string', 'max:32'],
            'notes'           => ['sometimes', 'nullable', 'string'],
        ];
    }
}
