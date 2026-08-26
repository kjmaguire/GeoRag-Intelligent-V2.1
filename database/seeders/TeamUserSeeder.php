<?php

declare(strict_types=1);

namespace Database\Seeders;

use App\Models\Project;
use App\Models\User;
use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seed the team user accounts (mtolmie, jmcgregor) with member access
 * to the existing project.
 *
 * Passwords are stored here as bcrypt hashes only — the plaintext
 * credentials were handed to the account holders out-of-band and are
 * deliberately NOT committed to the repository.
 *
 * Usage:
 *   docker exec georag-laravel-octane php artisan db:seed --class=TeamUserSeeder
 */
class TeamUserSeeder extends Seeder
{
    /**
     * Project both users are attached to as members (same project as
     * DemoUserSeeder).
     */
    private const PROJECT_ID = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

    /**
     * @var array<int, array{name: string, email: string, password: string}>
     */
    private const TEAM_USERS = [
        [
            'name' => 'M. Tolmie',
            'email' => 'mtolmie@georag.dev',
            'password' => '$2y$12$GzmXt2lw2pE7WwNjwcW7Qe/DhWiUlXT6iTngAXdoy9XJTPtZbIJgq',
        ],
        [
            'name' => 'J. McGregor',
            'email' => 'jmcgregor@georag.dev',
            'password' => '$2y$12$ELk1eL5gWhR8omlKjwzZrOh1S/OetcC7f1KIZZrJKanrUvr.g5mkG',
        ],
    ];

    public function run(): void
    {
        $projectExists = Project::query()
            ->whereKey(self::PROJECT_ID)
            ->exists();

        foreach (self::TEAM_USERS as $attributes) {
            $user = User::where('email', $attributes['email'])->first();

            if ($user === null) {
                // Insert via the query builder so the pre-computed bcrypt
                // hash is stored verbatim — the `hashed` cast rejects
                // hashes whose cost exceeds the configured rounds (e.g.
                // BCRYPT_ROUNDS=4 in the test environment).
                $userId = DB::table('users')->insertGetId([
                    'name' => $attributes['name'],
                    'email' => $attributes['email'],
                    'password' => $attributes['password'],
                    'is_admin' => false,
                    'created_at' => now(),
                    'updated_at' => now(),
                ]);
                $user = User::findOrFail($userId);
            }

            if (! $projectExists) {
                $this->command->warn(
                    'Project '.self::PROJECT_ID." not found — {$attributes['email']} created without project access.",
                );

                continue;
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

            $this->command->info(
                "Team user seeded: {$attributes['email']} (member of project ".self::PROJECT_ID.')',
            );
        }
    }
}
