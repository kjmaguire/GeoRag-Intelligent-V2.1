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
 * Verifies both team users (mtolmie, jmcgregor) are created as
 * non-admin members of the target project, that their pre-hashed
 * bcrypt passwords are stored verbatim (not double-hashed by the
 * `hashed` cast), that re-runs are idempotent, and that a missing
 * project degrades gracefully to user-only creation.
 */
class TeamUserSeederTest extends TestCase
{
    use RefreshDatabase;

    private const PROJECT_ID = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

    private const MTOLMIE_HASH = '$2y$12$GzmXt2lw2pE7WwNjwcW7Qe/DhWiUlXT6iTngAXdoy9XJTPtZbIJgq';

    private function seedTargetProject(): Project
    {
        return Project::factory()->create(['project_id' => self::PROJECT_ID]);
    }

    public function test_creates_both_users_as_project_members(): void
    {
        $this->seedTargetProject();

        $this->seed(TeamUserSeeder::class);

        foreach (['mtolmie@georag.dev', 'jmcgregor@georag.dev'] as $email) {
            $user = User::where('email', $email)->first();

            $this->assertNotNull($user, "Expected {$email} to be created");
            $this->assertFalse($user->is_admin, "{$email} must not be an admin");

            $this->assertDatabaseHas('project_user', [
                'user_id' => $user->id,
                'project_id' => self::PROJECT_ID,
                'role' => 'member',
            ]);
        }
    }

    public function test_stores_prehashed_password_without_rehashing(): void
    {
        $this->seedTargetProject();

        $this->seed(TeamUserSeeder::class);

        $stored = User::where('email', 'mtolmie@georag.dev')
            ->first()
            ->getAttributes()['password'];

        $this->assertSame(self::MTOLMIE_HASH, $stored);
    }

    public function test_reruns_are_idempotent(): void
    {
        $this->seedTargetProject();

        $this->seed(TeamUserSeeder::class);
        $this->seed(TeamUserSeeder::class);

        $this->assertSame(1, User::where('email', 'mtolmie@georag.dev')->count());
        $this->assertSame(1, User::where('email', 'jmcgregor@georag.dev')->count());

        $pivotCount = DB::table('project_user')
            ->where('project_id', self::PROJECT_ID)
            ->count();
        $this->assertSame(2, $pivotCount);
    }

    public function test_missing_project_creates_users_without_membership(): void
    {
        $this->seed(TeamUserSeeder::class);

        $this->assertSame(1, User::where('email', 'mtolmie@georag.dev')->count());
        $this->assertSame(1, User::where('email', 'jmcgregor@georag.dev')->count());
        $this->assertSame(0, DB::table('project_user')->count());
    }
}
