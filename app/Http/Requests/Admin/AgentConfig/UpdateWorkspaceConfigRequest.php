<?php

declare(strict_types=1);

namespace App\Http\Requests\Admin\AgentConfig;

use Illuminate\Foundation\Http\FormRequest;

/**
 * Validates a per-row update for /admin/agent-config/workspaces.
 *
 * `config` is a free-form JSON object — the wrapper merges it on top of
 * the global default at invocation time. We only assert that it is an
 * object (not a list) here; agent-specific schema validation belongs
 * inside each agent and is out of scope for this admin surface.
 */
class UpdateWorkspaceConfigRequest extends FormRequest
{
    public function authorize(): bool
    {
        return $this->user() !== null && (bool) $this->user()->is_admin;
    }

    /**
     * @return array<string, array<int, string>>
     */
    public function rules(): array
    {
        return [
            'enabled' => ['required', 'boolean'],
            'config' => ['present', 'array'],
        ];
    }
}
