<?php

declare(strict_types=1);

namespace App\Http\Requests;

use Illuminate\Foundation\Http\FormRequest;

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
            'filters.hole_type' => ['nullable', 'string', 'in:Diamond,RC,RAB,Rotary,Percussion'],
            'filters.status' => ['nullable', 'string', 'in:Active,Completed,Abandoned'],
            'filters.drill_date_from' => ['nullable', 'date'],
            'filters.drill_date_to' => ['nullable', 'date', 'after_or_equal:filters.drill_date_from'],
            'filters.min_depth' => ['nullable', 'numeric', 'min:0'],
            'filters.max_depth' => ['nullable', 'numeric', 'gt:filters.min_depth'],

            // csv_samples filters
            'filters.from_depth_min' => ['nullable', 'numeric', 'min:0'],
            'filters.from_depth_max' => ['nullable', 'numeric', 'gt:filters.from_depth_min'],
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

    public function messages(): array
    {
        return [
            'export_type.in' => 'export_type must be one of: csv_collars, csv_samples, csv_assays, csv_lithology, csv_geochem, csa_bundle, shapefile, geopackage, dxf, las_bundle.',
            'filters.review_status.in' => 'filters.review_status must be one of: accepted (default), include_pending, pending_only.',
        ];
    }
}
