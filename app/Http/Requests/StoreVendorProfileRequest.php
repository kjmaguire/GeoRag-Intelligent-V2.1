<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Models\VendorProfile;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

class StoreVendorProfileRequest extends FormRequest
{
    /**
     * Authorization is handled at the route level via auth:sanctum middleware.
     * Any authenticated user may create a vendor profile in Phase 1.
     */
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        return [
            'name'         => ['required', 'string', 'max:100', 'unique:vendor_profiles,name'],
            'description'  => ['nullable', 'string'],
            'profile_type' => ['required', Rule::in(VendorProfile::PROFILE_TYPES)],
            'is_global'    => ['required', 'boolean'],
        ];
    }

    public function messages(): array
    {
        return [
            'name.unique'          => 'A vendor profile with this name already exists.',
            'profile_type.in'      => 'profile_type must be one of: ' . implode(', ', VendorProfile::PROFILE_TYPES) . '.',
            'is_global.required'   => 'is_global is required (true or false).',
        ];
    }
}
