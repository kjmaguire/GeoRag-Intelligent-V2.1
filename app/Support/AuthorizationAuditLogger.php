<?php

declare(strict_types=1);

namespace App\Support;

use App\Models\User;
use Illuminate\Support\Facades\Log;

/**
 * Module 9 Chunk 9.8 — structured authorization-failure logger.
 *
 * Every 403 emitted by the IDOR gates (Chunk 9.1: ProjectController,
 * CollarController) and the FastAPI workspace-resolution failures (Chunk 9.4)
 * MUST surface to the operations log channel `authz_audit` with a fixed
 * structured payload: `actor_user_id`, `target_workspace_id`,
 * `target_resource`, `reason`. Pulse / Loki ingest from this channel.
 *
 * Why a dedicated logger, not the default channel
 * -----------------------------------------------
 * Operations needs a single grep to "show me every cross-tenant access
 * attempt in the last 24h" without scraping the noisy app log. This class
 * normalises the event shape and writes to the dedicated channel.
 *
 * Octane-safe: no per-instance state. Pure static method; the logger
 * resolves the channel each call so a config reload is honoured.
 */
final class AuthorizationAuditLogger
{
    /**
     * Log an authorization-denied event.
     *
     * @param User|int|string|null $actor Authenticated user, user-id int, or null.
     * @param string|null $targetResource Identifier of the resource that was denied (e.g. project UUID).
     * @param string $reason Short machine-readable reason code (e.g. "no_pivot_row", "cross_workspace").
     * @param string|null $targetWorkspaceId Workspace UUID the resource belongs to (when known).
     * @param array<string,mixed> $context Optional extra context (request path, IP, etc.).
     */
    public static function deny(
        User|int|string|null $actor,
        ?string $targetResource,
        string $reason,
        ?string $targetWorkspaceId = null,
        array $context = [],
    ): void {
        $actorUserId = match (true) {
            $actor instanceof User => (string) $actor->getKey(),
            is_null($actor) => null,
            default => (string) $actor,
        };

        Log::channel('authz_audit')->warning('authz.deny', array_merge([
            'event' => 'authz.deny',
            'actor_user_id' => $actorUserId,
            'target_workspace_id' => $targetWorkspaceId,
            'target_resource' => $targetResource,
            'reason' => $reason,
            'occurred_at' => now()->toIso8601String(),
        ], $context));
    }
}
