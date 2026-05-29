<?php

declare(strict_types=1);

namespace Database\Seeders;

use App\Models\User;
use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Hash;

/**
 * Seed a demo user with owner access to the existing project.
 *
 * Credentials:
 *   email:    demo@georag.dev
 *   password: georag2026
 *
 * Usage:
 *   docker exec georag-laravel-octane php artisan db:seed --class=DemoUserSeeder
 */
class DemoUserSeeder extends Seeder
{
    public function run(): void
    {
        $user = User::firstOrCreate(
            ['email' => 'demo@georag.dev'],
            [
                'name'     => 'Kyle Maguire',
                'password' => Hash::make('georag2026'),
                'is_admin' => true,
            ]
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
                'user_id'    => $user->id,
                'project_id' => $projectId,
                'role'       => 'owner',
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        }

        $this->command->info("Demo user seeded: demo@georag.dev / georag2026 (owner of project {$projectId})");
    }
}
