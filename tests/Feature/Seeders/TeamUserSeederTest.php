<?php

declare(strict_types=1);

namespace Tests\Feature\Seeders;

use App\Models\Project;
use App\Models\User;
use Database\Seeders\TeamUserSeeder;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * TeamUserSeeder behaviour.
 *
 * jmcgregor is a non-admin member of the target project, created once and
 * never touched again. mtolmie (reset 2026-09-30) is a global admin whose
 * committed hash and admin flag are enforced over an existing row, and who
 * owns every project that exists when the seeder runs. Pre-hashed passwords
 * are stored verbatim (not double-hashed by the `hashed` cast), re-runs are
 * idempotent, and a missing target project degrades gracefully.
 */
class TeamUserSeederTest extends TestCase
{
    use RefreshDatabase;

    private const PROJECT_ID = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

    private const MTOLMIE_HASH = '$2y$12$Xrffx9SVlHRWqMbIDBfCoOObu1Yk6jrML.WniemgHxAgLJ6UMGHEi';

    private const MTOLMIE_OLD_HASH = '$2y$12$GzmXt2lw2pE7WwNjwcW7Qe/DhWiUlXT6iTngAXdoy9XJTPtZbIJgq';

    private const JMCGREGOR_HASH = '$2y$12$ELk1eL5gWhR8omlKjwzZrOh1S/OetcC7f1KIZZrJKanrUvr.g5mkG';

    private function seedTargetProject(): Project
    {
        return Project::factory()->create(['project_id' => self::PROJECT_ID]);
    }

    private function storedPassword(string $email): string
    {
        return User::where('email', $email)->firstOrFail()->getAttributes()['password'];
    }

    public function test_jmcgregor_is_a_non_admin_member_of_the_target_project(): void
    {
        $this->seedTargetProject();

        $this->seed(TeamUserSeeder::class);

        $user = User::where('email', 'jmcgregor@georag.dev')->firstOrFail();
        $this->assertFalse($user->is_admin);
        $this->assertDatabaseHas('project_user', [
            'user_id' => $user->id,
            'project_id' => self::PROJECT_ID,
            'role' => 'member',
        ]);
        $this->assertSame(1, DB::table('project_user')->where('user_id', $user->id)->count());
    }

    public function test_mtolmie_is_an_admin_owning_every_project(): void
    {
        $target = $this->seedTargetProject();
        $other = Project::factory()->create();

        $this->seed(TeamUserSeeder::class);

        $user = User::where('email', 'mtolmie@georag.dev')->firstOrFail();
        $this->assertTrue($user->is_admin);
        foreach ([$target, $other] as $project) {
            $this->assertDatabaseHas('project_user', [
                'user_id' => $user->id,
                'project_id' => $project->project_id,
                'role' => 'owner',
            ]);
        }
        $this->assertTrue($user->hasProjectAccess((string) $other->project_id));
    }

    public function test_stores_prehashed_passwords_without_rehashing(): void
    {
        $this->seedTargetProject();

        $this->seed(TeamUserSeeder::class);

        $this->assertSame(self::MTOLMIE_HASH, $this->storedPassword('mtolmie@georag.dev'));
        $this->assertSame(self::JMCGREGOR_HASH, $this->storedPassword('jmcgregor@georag.dev'));
    }

    public function test_an_existing_mtolmie_is_reset_and_promoted(): void
    {
        $project = $this->seedTargetProject();
        $userId = DB::table('users')->insertGetId([
            'name' => 'M. Tolmie',
            'email' => 'mtolmie@georag.dev',
            'password' => self::MTOLMIE_OLD_HASH,
            'is_admin' => false,
            'created_at' => now(),
            'updated_at' => now(),
        ]);
        DB::table('project_user')->insert([
            'user_id' => $userId,
            'project_id' => $project->project_id,
            'role' => 'member',
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $this->seed(TeamUserSeeder::class);

        $this->assertSame(1, User::where('email', 'mtolmie@georag.dev')->count());
        $this->assertSame(self::MTOLMIE_HASH, $this->storedPassword('mtolmie@georag.dev'));
        $this->assertTrue(User::findOrFail($userId)->is_admin);
        $this->assertSame(
            ['owner'],
            DB::table('project_user')->where('user_id', $userId)->pluck('role')->all(),
        );
    }

    public function test_an_existing_jmcgregor_is_left_alone(): void
    {
        $this->seedTargetProject();
        DB::table('users')->insert([
            'name' => 'J. McGregor',
            'email' => 'jmcgregor@georag.dev',
            'password' => self::MTOLMIE_OLD_HASH,
            'is_admin' => false,
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $this->seed(TeamUserSeeder::class);

        $this->assertSame(self::MTOLMIE_OLD_HASH, $this->storedPassword('jmcgregor@georag.dev'));
    }

    public function test_reruns_are_idempotent(): void
    {
        $this->seedTargetProject();
        Project::factory()->create();

        $this->seed(TeamUserSeeder::class);
        $this->seed(TeamUserSeeder::class);

        $this->assertSame(1, User::where('email', 'mtolmie@georag.dev')->count());
        $this->assertSame(1, User::where('email', 'jmcgregor@georag.dev')->count());
        // mtolmie owns both projects; jmcgregor is a member of the target only.
        $this->assertSame(3, DB::table('project_user')->count());
    }

    public function test_missing_target_project_creates_users_without_membership(): void
    {
        $this->seed(TeamUserSeeder::class);

        $this->assertSame(1, User::where('email', 'mtolmie@georag.dev')->count());
        $this->assertSame(1, User::where('email', 'jmcgregor@georag.dev')->count());
        $this->assertSame(0, DB::table('project_user')->count());
    }
}
