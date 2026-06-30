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
