<?php

declare(strict_types=1);

namespace App\Http\Requests\Admin\AgentConfig;

use Illuminate\Foundation\Http\FormRequest;

/**
 * Validates pin/unpin for a single row of workspace.agent_prompt_pins.
 *
 * Setting prompt_version_id to null unpins the agent (the wrapper then
 * resolves the production-promoted version for that prompt_id at
 * invocation time). Cross-prompt mismatch is enforced in the controller
 * because it depends on the pin row's prompt_id, which the FormRequest
 * does not have access to.
 */
class UpdatePinRequest extends FormRequest
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
            'prompt_version_id' => ['nullable', 'uuid'],
        ];
    }
}
