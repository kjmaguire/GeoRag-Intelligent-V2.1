<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Models\VendorProfile;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

class UpdateVendorProfileRequest extends FormRequest
{
    /**
     * Authorization is enforced in VendorProfileController::update() via
     * Gate::authorize('admin') before this FormRequest is resolved. This
     * method returns true unconditionally so request validation can run
     * after the gate check succeeds.
     */
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        // The route-model binding resolves to a VendorProfile instance under
        // the key 'vendor_profile'. We ignore the bound ID so the unique rule
        // allows the current record to keep its own name unchanged.
        /** @var VendorProfile $profile */
        $profile = $this->route('vendor_profile');

        return [
            'name'         => [
                'sometimes',
                'required',
                'string',
                'max:100',
                Rule::unique('vendor_profiles', 'name')->ignore($profile->id),
            ],
            'description'  => ['sometimes', 'nullable', 'string'],
            'profile_type' => ['sometimes', 'required', Rule::in(VendorProfile::PROFILE_TYPES)],
            'is_global'    => ['sometimes', 'required', 'boolean'],
        ];
    }

    public function messages(): array
    {
        return [
            'name.unique'     => 'A vendor profile with this name already exists.',
            'profile_type.in' => 'profile_type must be one of: ' . implode(', ', VendorProfile::PROFILE_TYPES) . '.',
        ];
    }
}
