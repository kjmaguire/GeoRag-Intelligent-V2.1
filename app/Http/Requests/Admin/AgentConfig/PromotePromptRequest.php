<?php

declare(strict_types=1);

namespace App\Http\Requests\Admin\AgentConfig;

use Illuminate\Foundation\Http\FormRequest;

/**
 * Validates a promotion-state transition for a single prompt version.
 *
 * The CHECK constraint on workspace.prompt_versions.promotion_state
 * enforces the four allowed values. Phase 0 doesn't gate the order
 * of transitions (Phase 4 Prompt Release Approval Agent handles that);
 * any state in the allowed enum is accepted here.
 */
class PromotePromptRequest extends FormRequest
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
            'promotion_state' => ['required', 'string', 'in:draft,staging,production,deprecated'],
        ];
    }
}
