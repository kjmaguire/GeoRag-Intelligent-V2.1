<?php

declare(strict_types=1);

namespace Database\Seeders;

use App\Models\User;
use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Hash;
use RuntimeException;

/**
 * Seed a demo user with owner access to the existing project.
 *
 * Credentials:
 *   email:    demo@georag.dev
 *   password: georag2026
 *
 * LOCAL AND DEV ONLY. The account is a global admin (`is_admin`) and its
 * password is in this file, so on a public deployment it is an admin login
 * for anyone who has read the repository. It refuses to run when the
 * application environment is `production` (audit AWS-5, 2026-09-29), and
 * `.github/workflows/run-seeder.yml` no longer offers it.
 *
 * Usage:
 *   docker exec georag-laravel-octane php artisan db:seed --class=DemoUserSeeder
 */
class DemoUserSeeder extends Seeder
{
    public function run(): void
    {
        if (app()->environment('production')) {
            throw new RuntimeException(
                'DemoUserSeeder refuses to run in production: it creates a global admin '
                .'(demo@georag.dev) whose password is committed to the repository.',
            );
        }

        $user = User::firstOrCreate(
            ['email' => 'demo@georag.dev'],
            [
                'name' => 'Kyle Maguire',
                'password' => Hash::make('georag2026'),
                'is_admin' => true,
            ],
        );

        // Ensure the admin flag is set even if the row already existed.
        if (! $user->is_admin) {
            $user->update(['is_admin' => true]);
        }

        $projectId = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

        // Attach to project as owner if not already attached.
        $exists = DB::table('project_user')
            ->where('user_id', $user->id)
            ->where('project_id', $projectId)
            ->exists();

        if (! $exists) {
            DB::table('project_user')->insert([
                'user_id' => $user->id,
                'project_id' => $projectId,
                'role' => 'owner',
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        }

        $this->command->info("Demo user seeded: demo@georag.dev / georag2026 (owner of project {$projectId})");
    }
}
