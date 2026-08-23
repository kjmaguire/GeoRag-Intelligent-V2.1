<?php

declare(strict_types=1);

namespace App\Http\Requests;

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
            'company' => ['nullable', 'string', 'max:255'],
            'commodity' => ['nullable', 'string', 'max:50'],
            'region' => ['nullable', 'string', 'max:255'],
            'magnetic_declination' => ['nullable', 'numeric', 'between:-180,180'],
            'orientation_reference' => ['nullable', 'string', 'in:BOH,TOH'],
        ];
    }

    public function messages(): array
    {
        return [
            'project_name.required' => 'A project name is required.',
            'magnetic_declination.between' => 'Magnetic declination must be between -180 and 180 degrees.',
            'orientation_reference.in' => 'Orientation reference must be BOH or TOH.',
        ];
    }
}
