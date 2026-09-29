<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Models\Project;
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Validation\Rule;

/**
 * PATCH /api/v1/projects/{project} — backs the Overview's "Edit project" sheet.
 *
 * Authorization is not here: ProjectController::update() runs the same
 * project_user membership gate as destroy() (404 on no pivot row), so
 * authorize() stays permissive rather than duplicating it.
 *
 * What is deliberately NOT editable, because it is absent from rules() and so
 * never reaches validated():
 *
 * - `crs_epsg` — ingest_tabular reads it as the fallback CRS for CSVs and
 *   spreadsheets at ingest time. Changing it later does not reproject the
 *   collars already written, so the project would silently hold holes in two
 *   coordinate systems. Set it once, on create (StoreProjectRequest).
 * - `slug` — it is the /projects/{slug} URL and is unique; it is minted once
 *   in Project::booted() and a rename leaves it alone, so links keep working.
 * - `status` — cosmetic today (nothing writes or gates on it); the real
 *   lifecycle gate is `lifecycle_state`, read by FastAPI's
 *   project_lifecycle.py. A user-set "archived" would not stop ingestion.
 * - `workspace_id`, `project_id`, `data_version`, `lifecycle_state` —
 *   tenancy, identity and system-maintained state.
 *
 * The Overview sheet surfaces project_name, company, commodity, region and —
 * since Kyle's 2026-09-29 decision made desurvey apply them — the azimuth
 * reference (orientation_reference: BOH / TOH / grid / true / magnetic) and
 * magnetic_declination (degrees, EAST positive). A change takes effect on the
 * next promote_silver_to_gold run, which rebuilds every trace whose corrected
 * stations hash differently. crs_datum stays API-only.
 *
 * "magnetic needs a declination" is checked against the MERGED state (this
 * request plus the stored row) in ProjectController::update, after the
 * membership gate — not here, because a PATCH that only switches the
 * reference to magnetic is valid when a declination is already stored, and
 * reading the stored row during validation would run before authorization.
 */
class UpdateProjectRequest extends FormRequest
{
    public function authorize(): bool
    {
        return true;
    }

    public function rules(): array
    {
        return [
            'project_name' => ['sometimes', 'required', 'string', 'max:255'],
            'crs_datum' => ['nullable', 'string', 'max:50'],
            'company' => ['nullable', 'string', 'max:255'],
            'commodity' => ['nullable', 'string', 'max:50'],
            'region' => ['nullable', 'string', 'max:255'],
            // Degrees, EAST positive; null clears it.
            'magnetic_declination' => ['nullable', 'numeric', 'between:-180,180'],
            // Not nullable: the column is NOT NULL, so an explicit null
            // was a 500 on UPDATE. Omit the field to leave it unchanged.
            'orientation_reference' => ['sometimes', 'string', Rule::in(Project::ORIENTATION_REFERENCES)],
        ];
    }

    /**
     * The sheet's declination box is a text input; an emptied box arrives as
     * '' and means "not recorded", i.e. NULL — never 0.
     */
    protected function prepareForValidation(): void
    {
        if ($this->has('magnetic_declination') && trim((string) $this->input('magnetic_declination')) === '') {
            $this->merge(['magnetic_declination' => null]);
        }
    }

    public function messages(): array
    {
        return [
            'project_name.required' => 'A project name is required.',
            'magnetic_declination.between' => 'Magnetic declination must be between -180 and 180 degrees.',
            'orientation_reference.in' => 'Orientation reference must be BOH, TOH, grid, true or magnetic.',
        ];
    }
}
