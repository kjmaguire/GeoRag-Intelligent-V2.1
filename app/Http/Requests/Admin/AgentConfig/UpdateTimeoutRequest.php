<?php

declare(strict_types=1);

namespace App\Http\Requests\Admin\AgentConfig;

use Illuminate\Contracts\Validation\Validator;
use Illuminate\Foundation\Http\FormRequest;

/**
 * Validates the per-row UPDATE for /admin/agent-config/timeouts.
 *
 * Mirrors the CHECK constraints on workspace.agent_timeouts (Phase 0
 * Step 2 schema):
 *   - circuit_breaker_scope ∈ {none, workspace, global}
 *   - soft_timeout_ms <= hard_timeout_ms
 *   - risk_tier is intentionally NOT writable from this surface (it
 *     classifies the agent itself; changing it would change idempotency
 *     semantics, which is out of scope for an ops-tunable knob).
 */
class UpdateTimeoutRequest extends FormRequest
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
            'soft_timeout_ms' => ['required', 'integer', 'min:1', 'max:600000'],
            'hard_timeout_ms' => ['required', 'integer', 'min:1', 'max:600000'],
            'retry_count' => ['required', 'integer', 'min:0', 'max:10'],
            'circuit_breaker_scope' => ['required', 'string', 'in:none,workspace,global'],
            'failure_threshold' => ['required', 'integer', 'min:1', 'max:1000'],
            'cool_down_seconds' => ['required', 'integer', 'min:0', 'max:86400'],
        ];
    }

    public function withValidator(Validator $validator): void
    {
        $validator->after(function (Validator $v): void {
            $soft = $this->integer('soft_timeout_ms');
            $hard = $this->integer('hard_timeout_ms');
            if ($soft > 0 && $hard > 0 && $soft > $hard) {
                $v->errors()->add(
                    'soft_timeout_ms',
                    'soft_timeout_ms must be less than or equal to hard_timeout_ms.'
                );
            }
        });
    }
}
