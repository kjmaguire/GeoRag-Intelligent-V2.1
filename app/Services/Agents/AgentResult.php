<?php

declare(strict_types=1);

namespace App\Services\Agents;

class AgentResult
{
    public function __construct(
        public mixed $value,
        public string $outcome,    // success|refusal|failure|timeout|circuit_open|deduped
        public AgentContext $ctx,
        public int $durationMs,
        public bool $deduped = false,
        public ?string $error = null,
    ) {
    }
}
