<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use App\Models\Project;
use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Symfony\Component\HttpFoundation\Response;
use Throwable;

/**
 * Arm row-level security for the request.
 *
 * Every canonical RLS policy on the silver and gold tables reads
 * `current_setting('app.workspace_id', true)`, and that `true` means "return
 * NULL rather than error when unset" — which the policies then treat as
 * permissive. An unbound request sees every workspace. SetsWorkspaceRlsContext
 * says exactly this in its own docblock, and was correct but opt-in: 7 of the
 * 38 controllers under app/Http/Controllers used it. For the other 31,
 * tenancy rested entirely on whatever `where('project_id', ...)` the author
 * remembered, and two live cross-tenant IDOR bugs of that shape were found in
 * a single audit pass.
 *
 * ## Why a session GUC and not a transaction
 *
 * SetsWorkspaceRlsContext uses `SET LOCAL` inside an explicit transaction,
 * which is the correct shape under PgBouncer transaction pooling — only
 * within one transaction are all statements guaranteed the same backend.
 * Hoisting that into middleware would wrap every request in a transaction,
 * including the streaming query responses, and hold a pooled connection open
 * for the length of an LLM answer.
 *
 * It is also unnecessary here. Verified 2026-08-21: laravel-octane-cc
 * connects to `georag-pg-cc.postgres.database.azure.com:5432`, the direct
 * Postgres port. Azure's PgBouncer listens on 6432 and nothing points at it,
 * so each Laravel connection is a real session and a session-scoped GUC holds
 * for the whole request. `assertNotPooled()` fails loudly if that stops being
 * true, because the mechanism would then be unsound rather than merely
 * different.
 *
 * ## Why it always writes
 *
 * Octane reuses connections between requests. A GUC set for one request would
 * otherwise still be set for the next request on that worker — the same
 * cross-tenant read, just harder to reproduce. So this writes on every
 * request, including the ones it cannot resolve, where it writes the empty
 * string. No path leaves a previous request's value in place.
 */
class BindWorkspaceRlsContext
{
    /** PgBouncer's port on Azure Database for PostgreSQL Flexible Server. */
    private const POOLER_PORT = '6432';

    public function handle(Request $request, Closure $next): Response
    {
        $workspaceId = $this->resolveWorkspaceId($request);

        $this->bind($workspaceId);
        $request->attributes->set('workspace_id', $workspaceId);

        try {
            return $next($request);
        } finally {
            // Belt and braces. The next request rebinds anyway, but a
            // connection returned to Octane's pool should not be carrying a
            // tenant identity around with it.
            $this->bind(null);
        }
    }

    private function bind(?string $workspaceId): void
    {
        // set_config() is Postgres. The test suite runs on SQLite, where
        // there is no RLS to arm and the call would throw on every request
        // just to be caught and logged.
        //
        // Read from config rather than resolving the connection: this runs on
        // every request, and DB::connection() on a suite that swaps the
        // DatabaseManager for a mock raises BadMethodCallException from inside
        // the middleware stack — a middleware has no business being the
        // reason a mocked test 500s.
        if (! $this->isPostgres()) {
            return;
        }

        try {
            $this->assertNotPooled();
            DB::statement(
                "SELECT set_config('app.workspace_id', ?, false)",
                [$workspaceId ?? ''],
            );
        } catch (Throwable $e) {
            // A database that is down is the next query's problem to report,
            // not this middleware's. What must not happen is a request
            // proceeding while silently inheriting the previous tenant.
            Log::warning('BindWorkspaceRlsContext: could not bind app.workspace_id', [
                'event' => 'rls.bind_failed',
                'workspace_id' => $workspaceId,
                'exception' => $e->getMessage(),
            ]);
        }
    }

    private function isPostgres(): bool
    {
        $connection = (string) config('database.default');

        return in_array(
            (string) config("database.connections.{$connection}.driver"),
            ['pgsql', 'postgres', 'postgresql'],
            true,
        );
    }

    /**
     * A session GUC does not survive PgBouncer's transaction pooling.
     *
     * If the connection is ever repointed at the pooler, every statement in a
     * request can land on a different backend and this middleware becomes a
     * no-op that looks like protection. Say so.
     */
    private function assertNotPooled(): void
    {
        $connection = (string) config('database.default');
        if ((string) config("database.connections.{$connection}.port") === self::POOLER_PORT) {
            Log::critical(
                'app.workspace_id is bound as a SESSION GUC, but the pgsql '
                .'connection points at PgBouncer (port 6432). Under transaction '
                .'pooling the GUC does not survive between statements, so RLS is '
                .'effectively unarmed. Use SetsWorkspaceRlsContext::withWorkspaceRls '
                .'(SET LOCAL inside a transaction) instead.',
                ['event' => 'rls.pooled_connection'],
            );
        }
    }

    /**
     * Which tenant this request belongs to, or null when it cannot be known.
     *
     * Deliberately conservative. Guessing a workspace for a user who belongs
     * to several would bind the wrong one, which is worse than binding none:
     * an unbound request is visibly over-broad, a wrongly-bound one returns a
     * confidently empty page.
     */
    private function resolveWorkspaceId(Request $request): ?string
    {
        $user = $request->user();

        if ($user === null) {
            return null;
        }

        // 1. The route names a project. That covers the Foundry pages and the
        //    project-scoped API, which is most of the surface.
        $projectId = $this->projectIdFromRoute($request);
        if ($projectId !== null) {
            $workspaceId = $this->workspaceForProject($projectId);

            // Only if the user actually belongs to it. Reading the workspace
            // off a project the caller has no membership in would bind them
            // into someone else's tenant — the bug this exists to prevent.
            if ($workspaceId !== null && $this->userBelongsTo($user, $workspaceId)) {
                return $workspaceId;
            }

            return null;
        }

        // 2. The user belongs to exactly one workspace, so there is nothing
        //    to guess.
        $owned = $this->workspacesFor($user);

        return count($owned) === 1 ? $owned[0] : null;
    }

    private function projectIdFromRoute(Request $request): ?string
    {
        $route = $request->route();
        if ($route === null) {
            return null;
        }

        foreach (['project', 'project_id', 'projectId'] as $name) {
            $value = $route->parameter($name);
            if ($value instanceof Project) {
                return (string) $value->getKey();
            }
            if (is_string($value) && $value !== '') {
                return $value;
            }
        }

        return null;
    }

    private function workspaceForProject(string $projectId): ?string
    {
        // Short TTL: a project's workspace effectively never changes, and this
        // runs on every request. Long enough to matter, short enough that a
        // re-scoped project corrects itself without a deploy.
        return Cache::remember(
            "rls:project-workspace:{$projectId}",
            now()->addSeconds(60),
            static function () use ($projectId): ?string {
                try {
                    // Through the model, not raw SQL against silver.projects:
                    // the model knows its own table, which is what lets the
                    // test suite point it at an unqualified name on SQLite.
                    $workspaceId = Project::query()
                        ->whereKey($projectId)
                        ->value('workspace_id');
                } catch (Throwable) {
                    return null;
                }

                return $workspaceId !== null ? (string) $workspaceId : null;
            },
        );
    }

    private function userBelongsTo(mixed $user, string $workspaceId): bool
    {
        return in_array($workspaceId, $this->workspacesFor($user), true);
    }

    /**
     * The workspaces a user can reach, derived from the project_user pivot —
     * the same definition CitationController and PublicApiController use.
     *
     * @return list<string>
     */
    private function workspacesFor(mixed $user): array
    {
        try {
            return $user->projects()
                ->pluck('silver.projects.workspace_id')
                ->filter()
                ->map(static fn ($id): string => (string) $id)
                ->unique()
                ->values()
                ->all();
        } catch (Throwable) {
            return [];
        }
    }
}
