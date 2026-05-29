<?php

declare(strict_types=1);

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

class StoreProjectRequest extends FormRequest
{
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        return [
            'project_name'           => ['required', 'string', 'max:255'],
            'crs_datum'              => ['nullable', 'string', 'max:50'],
            'company'                => ['nullable', 'string', 'max:255'],
            'commodity'              => ['nullable', 'string', 'max:50'],
            'region'                 => ['nullable', 'string', 'max:255'],
            'magnetic_declination'   => ['nullable', 'numeric', 'between:-180,180'],
            'orientation_reference'  => ['nullable', 'string', 'in:BOH,TOH'],
        ];
    }

    public function messages(): array
    {
        return [
            'project_name.required'             => 'A project name is required.',
            'magnetic_declination.between'      => 'Magnetic declination must be between -180 and 180 degrees.',
            'orientation_reference.in'          => 'Orientation reference must be BOH or TOH.',
        ];
    }
}
