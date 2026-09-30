<?php

declare(strict_types=1);

namespace Database\Seeders;

use App\Models\Project;
use App\Models\User;
use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seed the team user accounts (mtolmie, jmcgregor).
 *
 * Passwords are stored here as bcrypt hashes only — the plaintext
 * credentials were handed to the account holders out-of-band and are
 * deliberately NOT committed to the repository.
 *
 * Two kinds of entry:
 *
 *  - `enforce => false` (jmcgregor): created if missing, never touched after
 *    that. A member of PROJECT_ID.
 *  - `enforce => true` (mtolmie, 2026-09-30): the committed hash and admin
 *    flag are the source of truth and are written over an existing row on
 *    every run — that is how the password is reset. Re-running the seeder
 *    after the holder changes their password in the app puts this one back.
 *
 * An admin entry is made OWNER of every project that exists when the seeder
 * runs. `is_admin` opens the admin surfaces only; project visibility comes
 * from project_user alone (User::hasProjectAccess), so an admin with no
 * pivot rows sees no projects. Projects created later are not picked up
 * until the seeder is run again.
 *
 * Usage (production): the "Run seeder" GitHub workflow (run-seeder.yml).
 * Usage (compose):
 *   docker exec georag-laravel-octane php artisan db:seed --class=TeamUserSeeder
 */
class TeamUserSeeder extends Seeder
{
    /**
     * Project non-admin entries are attached to as members (same project as
     * DemoUserSeeder).
     */
    private const PROJECT_ID = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

    /**
     * @var array<int, array{name: string, email: string, password: string, is_admin: bool, enforce: bool}>
     */
    private const TEAM_USERS = [
        [
            'name' => 'M. Tolmie',
            'email' => 'mtolmie@georag.dev',
            // Reset 2026-09-30; global admin at Kyle's request.
            'password' => '$2y$12$Xrffx9SVlHRWqMbIDBfCoOObu1Yk6jrML.WniemgHxAgLJ6UMGHEi',
            'is_admin' => true,
            'enforce' => true,
        ],
        [
            'name' => 'J. McGregor',
            'email' => 'jmcgregor@georag.dev',
            'password' => '$2y$12$ELk1eL5gWhR8omlKjwzZrOh1S/OetcC7f1KIZZrJKanrUvr.g5mkG',
            'is_admin' => false,
            'enforce' => false,
        ],
    ];

    public function run(): void
    {
        foreach (self::TEAM_USERS as $attributes) {
            $user = $this->upsertUser($attributes);

            if ($attributes['is_admin']) {
                $this->attachToEveryProjectAsOwner($user, $attributes['email']);

                continue;
            }

            $this->attachAsMember($user, $attributes['email']);
        }
    }

    /**
     * @param array{name: string, email: string, password: string, is_admin: bool, enforce: bool} $attributes
     */
    private function upsertUser(array $attributes): User
    {
        $user = User::where('email', $attributes['email'])->first();

        // The query builder, not the model, so the pre-computed bcrypt hash is
        // stored verbatim — the `hashed` cast rejects hashes whose cost exceeds
        // the configured rounds (e.g. BCRYPT_ROUNDS=4 in the test environment).
        if ($user === null) {
            $userId = DB::table('users')->insertGetId([
                'name' => $attributes['name'],
                'email' => $attributes['email'],
                'password' => $attributes['password'],
                'is_admin' => $attributes['is_admin'],
                'created_at' => now(),
                'updated_at' => now(),
            ]);

            return User::findOrFail($userId);
        }

        if ($attributes['enforce']) {
            DB::table('users')->where('id', $user->id)->update([
                'password' => $attributes['password'],
                'is_admin' => $attributes['is_admin'],
                'updated_at' => now(),
            ]);
            $this->command->info("Team user updated: {$attributes['email']} (password and admin flag reset)");

            return User::findOrFail($user->id);
        }

        return $user;
    }

    private function attachAsMember(User $user, string $email): void
    {
        if (! Project::query()->whereKey(self::PROJECT_ID)->exists()) {
            $this->command->warn('Project '.self::PROJECT_ID." not found — {$email} has no project access.");

            return;
        }

        $attached = DB::table('project_user')
            ->where('user_id', $user->id)
            ->where('project_id', self::PROJECT_ID)
            ->exists();

        if (! $attached) {
            DB::table('project_user')->insert([
                'user_id' => $user->id,
                'project_id' => self::PROJECT_ID,
                'role' => 'member',
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        }

        $this->command->info("Team user seeded: {$email} (member of project ".self::PROJECT_ID.')');
    }

    private function attachToEveryProjectAsOwner(User $user, string $email): void
    {
        $projectIds = Project::query()->pluck('project_id');

        foreach ($projectIds as $projectId) {
            $existing = DB::table('project_user')
                ->where('user_id', $user->id)
                ->where('project_id', $projectId);

            if ($existing->exists()) {
                $existing->update(['role' => 'owner', 'updated_at' => now()]);

                continue;
            }

            DB::table('project_user')->insert([
                'user_id' => $user->id,
                'project_id' => $projectId,
                'role' => 'owner',
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        }

        $this->command->info("Team user seeded: {$email} (admin, owner of {$projectIds->count()} project(s))");
    }
}
