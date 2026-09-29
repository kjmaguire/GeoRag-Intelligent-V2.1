<?php

declare(strict_types=1);

namespace App\Http\Requests;

use App\Models\Project;
use Illuminate\Foundation\Http\FormRequest;

class StoreProjectRequest extends FormRequest
{
    /**
     * Only someone who already belongs to a tenant may create a project.
     *
     * This returned `true` unconditionally, which on its own was survivable
     * — the route is behind auth:sanctum. It stopped being survivable in
     * combination with open registration and a hardcoded workspace fallback
     * in the controller: register, POST /projects, and you owned a project
     * inside the real production tenant, which then unlocked its audit
     * ledger, its usage rollups and its private Reverb activity channel.
     *
     * Membership is derived the same way every other Api/V1 controller
     * derives it — the project_user pivot. A fresh account has none, so it
     * cannot create anything. An admin can, which is how the first project
     * in a new deployment gets made.
     */
    public function authorize(): bool
    {
        $user = $this->user();

        if ($user === null) {
            return false;
        }

        if ($user->is_admin) {
            return true;
        }

        return $user->projects()->exists();
    }

    public function rules(): array
    {
        return [
            'project_name' => ['required', 'string', 'max:255'],
            // Optional, and only honoured for a workspace the creator
            // belongs to (or any workspace, for an admin bootstrapping a
            // fresh deployment). See ProjectController::resolveWorkspaceId().
            'workspace_id' => ['nullable', 'uuid'],
            'crs_datum' => ['nullable', 'string', 'max:50'],
            // The project's coordinate system as an EPSG CODE, and the
            // fallback ingest_tabular reads when a CSV or spreadsheet does
            // not carry its own. Same 1024-32767 bound as
            // StoreQueryRequest's context_envelope.crs_epsg and the CHECK on
            // silver.spatial_features.crs_epsg_native — a fourth definition
            // of "a valid CRS" is how the three that already exist drift.
            //
            // Deliberately separate from `crs_datum`, which is free text
            // ('EPSG:32613', 'NAD83 / UTM 13N', whatever was typed) and
            // cannot be parsed back into a number reliably enough to
            // reproject coordinates with.
            'crs_epsg' => ['nullable', 'integer', 'min:1024', 'max:32767'],
            'company' => ['nullable', 'string', 'max:255'],
            'commodity' => ['nullable', 'string', 'max:50'],
            'region' => ['nullable', 'string', 'max:255'],
            'magnetic_declination' => ['nullable', 'numeric', 'between:-180,180'],
            // Always present after prepareForValidation() — see there.
            'orientation_reference' => ['required', 'string', 'in:BOH,TOH'],
        ];
    }

    /**
     * Default orientation_reference to BOH when the client omits it or
     * sends null.
     *
     * silver.projects.orientation_reference is NOT NULL, so a "nullable"
     * rule let an API client that left it out through validation and into
     * a 500 on INSERT (database audit 2026-09-29 PG-14). BOH is what every
     * other writer uses: the New Project form sends it, FastAPI's Project
     * model defaults to it, and since 2026-09-29 the ingestion project
     * stubs write it too (they wrote 'grid_north'). The column also has a
     * DB default of BOH (2026_09_29_210400).
     *
     * Vocabulary: BOH / TOH is the core-orientation mark convention
     * (bottom- / top-of-hole). 'grid_north', found on projects created by
     * the LAS / cluster ingestion stubs before 2026-09-29, is a north
     * reference, not an orientation mark — it carries no orientation
     * information and nothing downstream applies this column (see
     * UpdateProjectRequest). Those rows are left as they are.
     */
    protected function prepareForValidation(): void
    {
        if ($this->input('orientation_reference') === null) {
            $this->merge(['orientation_reference' => Project::DEFAULT_ORIENTATION_REFERENCE]);
        }
    }

    public function messages(): array
    {
        return [
            'project_name.required' => 'A project name is required.',
            'crs_epsg.min' => 'EPSG codes must be in the range 1024-32767.',
            'crs_epsg.max' => 'EPSG codes must be in the range 1024-32767.',
            'magnetic_declination.between' => 'Magnetic declination must be between -180 and 180 degrees.',
            'orientation_reference.in' => 'Orientation reference must be BOH or TOH.',
        ];
    }
}
