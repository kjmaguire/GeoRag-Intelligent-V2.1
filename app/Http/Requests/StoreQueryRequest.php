<?php

declare(strict_types=1);

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

class StoreQueryRequest extends FormRequest
{
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        return [
            'query' => ['required', 'string', 'max:2000'],
            'project_id' => [
                'required',
                'uuid',
                function ($attribute, $value, $fail) {
                    $exists = \DB::table('silver.projects')
                        ->where('project_id', $value)
                        ->exists();
                    if (! $exists) {
                        $fail('The specified project does not exist.');
                    }
                },
            ],

            // Phase 3 / Step 3.2 + 3.3 — optional context envelope from the
            // query-builder UI. All 12 fields are optional; the FastAPI side
            // treats missing ones as unspecified per Phase 2.4.
            'context_envelope' => ['sometimes', 'array'],
            'context_envelope.area_of_interest' => ['nullable', 'string', 'max:500'],
            'context_envelope.crs_epsg' => ['nullable', 'integer', 'min:1024', 'max:32767'],
            'context_envelope.depth_reference' => ['nullable', 'in:bgl,asl,rl,tvd,md'],
            'context_envelope.scale_resolution' => ['nullable', 'string', 'max:64'],
            'context_envelope.stratigraphic_frame' => ['nullable', 'string', 'max:200'],
            'context_envelope.specific_objects' => ['nullable', 'array'],
            'context_envelope.specific_objects.*' => ['string', 'max:128'],
            'context_envelope.data_sources' => ['nullable', 'array'],
            'context_envelope.data_sources.*' => [
                'string',
                'in:drill_logs,assays,technical_reports,maps,geophysics,public_geoscience',
            ],
            'context_envelope.qaqc_constraints' => ['nullable', 'string', 'max:500'],
            'context_envelope.units_and_detection_limits' => ['nullable', 'string', 'max:500'],
            'context_envelope.reporting_code' => [
                'nullable',
                'in:NI 43-101,CIM,CRIRSCO,JORC,SAMREC,PERC',
            ],
            'context_envelope.decision_to_support' => ['nullable', 'string', 'max:500'],
            'context_envelope.desired_output_structure' => ['nullable', 'string', 'max:200'],
            'context_envelope.mode' => ['nullable', 'in:field,office'],
        ];
    }

    public function messages(): array
    {
        return [
            'query.required' => 'A query string is required.',
            'query.max' => 'Query must not exceed 2000 characters.',
            'project_id.required' => 'A project ID is required.',
            'project_id.uuid' => 'Project ID must be a valid UUID.',
            'project_id.exists' => 'The specified project does not exist.',
            'context_envelope.crs_epsg.min' => 'EPSG codes must be in the range 1024-32767.',
            'context_envelope.crs_epsg.max' => 'EPSG codes must be in the range 1024-32767.',
            'context_envelope.depth_reference.in' => 'Depth reference must be one of: bgl, asl, rl, tvd, md.',
            'context_envelope.reporting_code.in' => 'Reporting code must be one of: NI 43-101, CIM, CRIRSCO, JORC, SAMREC, PERC.',
            'context_envelope.mode.in' => 'Mode must be "field" or "office".',
        ];
    }
}
