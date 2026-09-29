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
use Symfony\Component\HttpKernel\Exception\HttpException;
use Symfony\Component\HttpKernel\Exception\NotFoundHttpException;
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
 * ## Why a session GUC, and why it refuses to run behind a pooler
 *
 * SetsWorkspaceRlsContext uses `SET LOCAL` inside an explicit transaction,
 * which is the correct shape under transaction pooling — only within one
 * transaction are all statements guaranteed the same backend. Hoisting that
 * into middleware would wrap every request in one transaction: a query error
 * a controller catches and carries on from would abort the rest of the
 * request (25P02), a StreamedResponse body runs after this middleware has
 * already returned, and the pooled backend is held for the whole request.
 *
 * So this binds per SESSION, which is only sound when every Laravel
 * connection is a real Postgres session: the AWS deployment (no pooler —
 * config.tf points Laravel straight at RDS:5432), compose and the Helm
 * chart (both point Laravel at Postgres directly since SEC-2). Behind a
 * transaction pooler a session GUC set by one statement stays on a backend
 * the next statement — or the next tenant's request — may land on, and
 * PgBouncer does not run server_reset_query in transaction mode. That is a
 * cross-tenant read, not a no-op, so refusePooled() fails CLOSED with a 503
 * on every request when `database.connections.<default>.pooled`
 * (DB_POOLED) is true. It is an explicit flag, not port sniffing: RDS Proxy
 * listens on 5432. Port 6432 is still treated as pooled, as a second
 * signal, because refusing costs nothing there.
 *
 * ## Why it always writes
 *
 * Octane reuses connections between requests. A GUC set for one request would
 * otherwise still be set for the next request on that worker — the same
 * cross-tenant read, just harder to reproduce. So this writes on every
 * request, including the ones it cannot resolve, where it writes the empty
 * string. No path leaves a previous request's value in place.
 *
 * ## Why a failed bind is a 503
 *
 * Most RLS policies in this cluster are still fail-open (see
 * docs/architecture/fail-open-rls-posture-2026-08-21.md): an unset or empty
 * `app.workspace_id` admits every workspace's rows. A request allowed past a
 * failed bind therefore runs with tenancy disarmed — or silently inherits
 * whatever the previous request on this Octane worker bound. bind() refuses:
 * it throws a 503 before the controller runs and severs the connection so
 * the stale session cannot be handed to the next request either.
 */
class BindWorkspaceRlsContext
{
    /** PgBouncer's conventional port — a secondary signal; DB_POOLED is the contract. */
    private const POOLER_PORT = '6432';

    public function handle(Request $request, Closure $next): Response
    {
        $this->refusePooled();

        $workspaceId = $this->resolveWorkspaceId($request);

        $this->bind($workspaceId);
        $request->attributes->set('workspace_id', $workspaceId);

        try {
            return $next($request);
        } finally {
            try {
                // Belt and braces. The next request rebinds anyway, but a
                // connection returned to Octane's pool should not be carrying
                // a tenant identity around with it.
                $this->bind(null);
            } catch (Throwable) {
                // bind() has already logged and severed the connection. The
                // response in flight was produced under a correctly bound
                // GUC; replacing it with a 503 because CLEANUP failed would
                // fail good requests, and the disconnect already guarantees
                // no later request can inherit this session's tenant.
            }
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
            DB::statement(
                "SELECT set_config('app.workspace_id', ?, false)",
                [$workspaceId ?? ''],
            );
        } catch (Throwable $e) {
            // Fail CLOSED. What must not happen is a request proceeding while
            // silently inheriting the previous tenant — and on the fail-open
            // policy shape that still covers most of this cluster, an unset
            // or empty GUC is worse than a stale one: it admits EVERY
            // workspace's rows. A database that is down surfaces as a 5xx
            // either way; this way it cannot surface as someone else's data.
            Log::error('BindWorkspaceRlsContext: could not bind app.workspace_id — refusing to serve the request', [
                'event' => 'rls.bind_failed',
                'workspace_id' => $workspaceId,
                'exception' => $e->getMessage(),
            ]);

            $this->severConnection();

            throw new HttpException(
                503,
                'Row-level security could not be armed for this request; '
                .'refusing to serve it without tenant isolation. '
                .'(log event: rls.bind_failed)',
                $e,
            );
        }
    }

    /**
     * A session whose GUC state is unknown must not go back into Octane's
     * pool: the next request's resolveWorkspaceId() reads silver.projects
     * BEFORE its own bind() runs, so a stale tenant value would be live for
     * exactly that window. Disconnecting guarantees the next request starts
     * from a fresh session.
     *
     * Guarded on the LIVE connection's driver, not isPostgres(): under the
     * test suite the config can claim pgsql while the resolved connection is
     * still SQLite, and dropping an in-memory SQLite connection destroys the
     * schema mid-test.
     */
    private function severConnection(): void
    {
        try {
            $connection = DB::connection();
            if ($connection->getDriverName() === 'pgsql') {
                $connection->disconnect();
            }
        } catch (Throwable) {
            // Best effort. If the connection cannot even be resolved, it is
            // not going back into the pool carrying a GUC either.
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
     * A session GUC is unsound behind a transaction pooler — refuse, do not
     * warn.
     *
     * This used to Log::critical and carry on, which under PgBouncer's
     * transaction mode meant binding tenant X on one backend and running the
     * controller's queries on whichever backend came next — possibly one
     * still carrying tenant Y from an earlier request (SEC-2). Nothing is
     * written to the session before this runs, so there is nothing to sever.
     */
    private function refusePooled(): void
    {
        if (! $this->isPostgres() || ! $this->isPooled()) {
            return;
        }

        Log::critical(
            'BindWorkspaceRlsContext: the pgsql connection is behind a transaction '
            .'pooler (DB_POOLED=true or port 6432). app.workspace_id is bound per '
            .'session and would not follow the request across backends, so every '
            .'request is refused. Point Laravel at Postgres directly.',
            ['event' => 'rls.pooled_connection'],
        );

        throw new HttpException(
            503,
            'Row-level security cannot be armed through a transaction pooler; '
            .'refusing to serve this request without tenant isolation. '
            .'(log event: rls.pooled_connection)',
        );
    }

    private function isPooled(): bool
    {
        $connection = (string) config('database.default');

        return (bool) config("database.connections.{$connection}.pooled", false)
            || (string) config("database.connections.{$connection}.port") === self::POOLER_PORT;
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

        // 1. The route names a project — by id on the project API, by slug
        //    on every Foundry page (/projects/{slug}/...). Most of the
        //    surface. Slug routes used to fall through to step 2 (SEC-8).
        $project = $this->projectFromRoute($request);
        if ($project !== null) {
            $workspaceId = $project['workspace_id'];

            // A project with no workspace cannot be bound, and binding ''
            // instead is fail-open on most policies. Refuse rather than serve
            // it with RLS disarmed (SEC-10): 409 to a member, the same answer
            // RasterLayersController::workspaceIdOrFail() gives; 404 to anyone
            // else, so the refusal is not an existence oracle. Postgres only:
            // SQLite has no RLS to disarm. (ProjectFactory gives every project
            // a workspace; WorkspaceBindFailsClosedTest opts out to test this.)
            if ($workspaceId === null) {
                if ($this->isPostgres()) {
                    if ($this->userIsMemberOf($user, $project['project_id'])) {
                        throw new HttpException(409, 'This project has no workspace assigned, so tenant-scoped data cannot be read safely.');
                    }

                    throw new NotFoundHttpException('Project not found.');
                }

                return null;
            }

            // Only if the user actually belongs to it. Reading the workspace
            // off a project the caller has no membership in would bind them
            // into someone else's tenant — the bug this exists to prevent.
            if ($this->userBelongsTo($user, $workspaceId)) {
                return $workspaceId;
            }

            return null;
        }

        // 2. The user belongs to exactly one workspace, so there is nothing
        //    to guess.
        $owned = $this->workspacesFor($user);

        return count($owned) === 1 ? $owned[0] : null;
    }

    /**
     * The project the route names, or null when it names none or the project
     * does not exist (the controller's own 404 handles that).
     *
     * `{slug}` is a project slug on every route that declares it, and
     * silver.projects.slug is globally unique (projects_slug_unique).
     *
     * @return array{project_id: string, workspace_id: ?string}|null
     */
    private function projectFromRoute(Request $request): ?array
    {
        $route = $request->route();
        if ($route === null) {
            return null;
        }

        foreach (['project', 'project_id', 'projectId'] as $name) {
            $value = $route->parameter($name);
            if ($value instanceof Project) {
                return [
                    'project_id' => (string) $value->getKey(),
                    'workspace_id' => $value->workspace_id !== null ? (string) $value->workspace_id : null,
                ];
            }
            if (is_string($value) && $value !== '') {
                return $this->lookupProject('project_id', $value);
            }
        }

        $slug = $route->parameter('slug');
        if (is_string($slug) && $slug !== '') {
            return $this->lookupProject('slug', $slug);
        }

        return null;
    }

    /**
     * @param 'project_id'|'slug' $column
     *
     * @return array{project_id: string, workspace_id: ?string}|null
     */
    private function lookupProject(string $column, string $value): ?array
    {
        // Short TTL: a project's workspace effectively never changes, and this
        // runs on every request. Long enough to matter, short enough that a
        // re-scoped project corrects itself without a deploy. A missing
        // project returns null, which Cache::remember does not store.
        return Cache::remember(
            "rls:project-workspace:{$column}:{$value}",
            now()->addSeconds(60),
            static function () use ($column, $value): ?array {
                try {
                    // Through the model, not raw SQL against silver.projects:
                    // the model knows its own table, which is what lets the
                    // test suite point it at an unqualified name on SQLite.
                    $row = Project::query()
                        ->where($column === 'slug' ? 'slug' : (new Project)->getKeyName(), $value)
                        ->first(['project_id', 'workspace_id']);
                } catch (Throwable) {
                    return null;
                }

                if ($row === null) {
                    return null;
                }

                $workspaceId = $row->getAttribute('workspace_id');

                return [
                    'project_id' => (string) $row->getAttribute('project_id'),
                    'workspace_id' => $workspaceId !== null && $workspaceId !== '' ? (string) $workspaceId : null,
                ];
            },
        );
    }

    private function userIsMemberOf(mixed $user, string $projectId): bool
    {
        try {
            return $user->projects()
                ->where('silver.projects.project_id', $projectId)
                ->exists();
        } catch (Throwable) {
            return false;
        }
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
