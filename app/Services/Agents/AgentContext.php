<?php

declare(strict_types=1);

namespace App\Services\Agents;

use Ramsey\Uuid\Uuid;

/**
 * Per-invocation context — mirror of src/fastapi/app/agents/context.py.
 */
class AgentContext
{
    public string $invocationId;

    public string $agentName = '';

    public string $agentVersion = '';

    public string $riskTier = 'R0';

    /** @var array<string, mixed> */
    public array $usage = [];

    public function __construct(
        public ?string $workspaceId = null,
        public ?int $actorId = null,
        public string $actorKind = 'system',
        public ?string $traceId = null,
        // Tier-specific fields used by the idempotency-key recipe.
        public ?string $documentId = null,
        public ?string $exportRequestId = null,
        public ?string $syncTarget = null,
        public ?string $syncRequestId = null,
        public ?string $targetId = null,
        public ?string $signoffSessionId = null,
        public bool $dryRun = false,
        public bool $bypassIdempotency = false,
        ?string $invocationId = null,
    ) {
        $this->invocationId = $invocationId ?? Uuid::uuid4()->toString();
    }

    public function isDryRun(): bool
    {
        return $this->dryRun && in_array($this->riskTier, ['R3', 'R4', 'R5'], true);
    }
}
