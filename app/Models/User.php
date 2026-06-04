<?php

declare(strict_types=1);

namespace App\Models;

use Database\Factories\UserFactory;
use Illuminate\Database\Eloquent\Attributes\Fillable;
use Illuminate\Database\Eloquent\Attributes\Hidden;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Relations\BelongsToMany;
use Illuminate\Database\QueryException;
use Illuminate\Foundation\Auth\User as Authenticatable;
use Illuminate\Notifications\Notifiable;
use Illuminate\Support\Facades\Log;
use Laravel\Sanctum\HasApiTokens;

#[Fillable(['name', 'email', 'password', 'is_admin'])]
#[Hidden(['password', 'remember_token'])]
class User extends Authenticatable
{
    /** @use HasFactory<UserFactory> */
    use HasApiTokens;
    use HasFactory;
    use Notifiable;

    /**
     * The projects this user has access to.
     */
    public function projects(): BelongsToMany
    {
        return $this->belongsToMany(Project::class, 'project_user', 'user_id', 'project_id')
            ->withPivot('role')
            ->withTimestamps();
    }

    /**
     * The workspaces this user belongs to, with role.
     *
     * Added 2026-06-03 audit (item A) — closes the
     * `$user->workspace_id ?? <default>` cluster of bugs. With this
     * relationship in place, controllers and middleware can resolve
     * the user's workspace context directly rather than guessing
     * from project memberships.
     *
     * Returns DB rows (not a Workspace Eloquent model, because the
     * silver.workspaces table is read raw — there's no Workspace
     * model in the codebase yet). The pivot carries role + timestamps.
     *
     * Backfilled from project_user on the 2026_06_03_020000 migration:
     * every user with a project_user row to a project in workspace X
     * gets a workspace_user(X) row whose role is the highest project-
     * level role they hold in that workspace.
     */
    public function workspaces(): BelongsToMany
    {
        // belongsToMany against a raw table since silver.workspaces has
        // no Eloquent model. The related "model" stays as User just to
        // satisfy the type — code paths use ->pivot to read role and
        // ->workspace_id directly via the join.
        //
        // Actual query: SELECT silver.workspaces.* FROM silver.workspaces
        // INNER JOIN workspace_user ON ... WHERE user_id = ?
        // (Project model uses the same pattern — Project::class extends
        // Model but binds to silver.projects via $table.)
        return $this->belongsToMany(
            Workspace::class,
            'workspace_user',
            'user_id',
            'workspace_id',
        )->withPivot('role')->withTimestamps();
    }

    /**
     * True if the user is a member of the given workspace.
     *
     * Fails CLOSED on DB error (mirrors hasProjectAccess). Specifically
     * tolerates the "workspace_user table missing" case (e.g., the
     * migration hasn't run yet on a particular environment) by
     * deferring to the legacy "any project in this workspace" check.
     */
    public function hasWorkspaceAccess(string $workspaceId): bool
    {
        try {
            return $this->workspaces()
                ->where('silver.workspaces.workspace_id', $workspaceId)
                ->exists();
        } catch (QueryException $e) {
            if (self::isMissingWorkspaceUserPivot($e)) {
                Log::critical(
                    'hasWorkspaceAccess: workspace_user pivot table missing — '
                    .'falling back to project-membership derivation. Run `php artisan migrate`.',
                    [
                        'user_id' => $this->getKey(),
                        'workspace_id' => $workspaceId,
                        'exception' => $e->getMessage(),
                    ],
                );
                // Fail-safe derivation: any project in this workspace
                // implies membership. Matches the pre-migration behavior
                // of the call sites this method replaces.
                try {
                    return $this->projects()
                        ->where('silver.projects.workspace_id', $workspaceId)
                        ->exists();
                } catch (QueryException) {
                    return false;
                }
            }
            throw $e;
        }
    }

    /**
     * Return the user's role in the given workspace, or null if not a member.
     *
     * Values: 'owner' | 'admin' | 'member' | 'viewer' | null
     */
    public function workspaceRole(string $workspaceId): ?string
    {
        try {
            $ws = $this->workspaces()
                ->where('silver.workspaces.workspace_id', $workspaceId)
                ->first();

            return $ws?->pivot?->role;
        } catch (QueryException) {
            return null;
        }
    }

    /**
     * Default workspace_id for this user.
     *
     * Used by HandleInertiaRequests to seed `auth.user.current_workspace_id`
     * on every page load when the session doesn't carry an explicit
     * selection. Returns the workspace_id of the user's earliest-joined
     * workspace (ORDER BY workspace_user.created_at ASC), or null when
     * the user belongs to no workspace yet (brand-new account before
     * onboarding completes).
     */
    public function defaultWorkspaceId(): ?string
    {
        try {
            $row = $this->workspaces()
                ->orderBy('workspace_user.created_at', 'asc')
                ->first();

            return $row?->workspace_id ? (string) $row->workspace_id : null;
        } catch (QueryException $e) {
            if (self::isMissingWorkspaceUserPivot($e)) {
                // Pre-migration fallback — derive from first project's workspace.
                try {
                    return (string) $this->projects()
                        ->orderBy('silver.projects.created_at', 'asc')
                        ->value('silver.projects.workspace_id') ?: null;
                } catch (QueryException) {
                    return null;
                }
            }

            return null;
        }
    }

    private static function isMissingWorkspaceUserPivot(QueryException $e): bool
    {
        $sqlstate = $e->getCode();
        if ($sqlstate !== '42P01' && (int) $sqlstate !== 1146) {
            return false;
        }

        return str_contains($e->getMessage(), 'workspace_user');
    }

    /**
     * Check if the user has access to a specific project.
     *
     * Fails CLOSED on any database error, including a missing `project_user`
     * pivot table. If the pivot is absent (e.g. migrations have not run),
     * we log a CRITICAL alert and return false — denying all access — rather
     * than granting it. A boot-time guard in AppServiceProvider also refuses
     * to start the web process when the pivot is missing, so in practice this
     * catch branch only fires if the table is dropped while the worker is
     * running. Any OTHER QueryException re-throws so we don't silently
     * swallow unrelated DB outages.
     */
    public function hasProjectAccess(string $projectId): bool
    {
        try {
            return $this->projects()
                ->where('silver.projects.project_id', $projectId)
                ->exists();
        } catch (QueryException $e) {
            if (self::isMissingProjectUserPivot($e)) {
                Log::critical('hasProjectAccess: project_user pivot table missing or unreadable — denying access (fail-CLOSED). Run `php artisan migrate`.', [
                    'user_id' => $this->getKey(),
                    'project_id' => $projectId,
                    'exception' => $e->getMessage(),
                ]);

                return false;  // fail-CLOSED
            }
            throw $e;
        }
    }

    /**
     * Check if the user is the owner of a project. Already fails closed —
     * no change required. Pivot-absence returns false (not an owner).
     */
    public function isProjectOwner(string $projectId): bool
    {
        try {
            return $this->projects()
                ->where('silver.projects.project_id', $projectId)
                ->wherePivot('role', 'owner')
                ->exists();
        } catch (QueryException $e) {
            if (self::isMissingProjectUserPivot($e)) {
                return false; // Safer default for "ownership" checks.
            }
            throw $e;
        }
    }

    /**
     * Recognise the specific Postgres error code + table name that means
     * "project_user pivot doesn't exist". Anything else re-raises.
     */
    private static function isMissingProjectUserPivot(QueryException $e): bool
    {
        // SQLSTATE 42P01 = undefined_table (postgres) and 1146 on MySQL.
        $sqlstate = $e->getCode();
        if ($sqlstate !== '42P01' && (int) $sqlstate !== 1146) {
            return false;
        }

        return str_contains($e->getMessage(), 'project_user');
    }

    /**
     * Get the attributes that should be cast.
     *
     * @return array<string, string>
     */
    protected function casts(): array
    {
        return [
            'email_verified_at' => 'datetime',
            'password' => 'hashed',
            'is_admin' => 'boolean',
        ];
    }
}
