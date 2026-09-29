<?php

declare(strict_types=1);

namespace Tests\Feature\Seeders;

use App\Models\Project;
use App\Models\User;
use Database\Seeders\DemoUserSeeder;
use Illuminate\Foundation\Testing\RefreshDatabase;
use RuntimeException;
use Tests\TestCase;

/**
 * DemoUserSeeder must never create its account in production.
 *
 * The account is a global admin whose password is committed to the
 * repository, so on the public deployment it would be an admin login for
 * anyone who has read the code (audit AWS-5, 2026-09-29).
 */
class DemoUserSeederTest extends TestCase
{
    use RefreshDatabase;

    public function test_refuses_to_run_in_production_and_creates_nothing(): void
    {
        $this->app['env'] = 'production';

        try {
            // Called directly: `db:seed` itself stops at a confirmation
            // prompt in production, which is not the guard under test —
            // run-seeder.yml passes --force past that prompt.
            $this->app->make(DemoUserSeeder::class)->run();
            $this->fail('DemoUserSeeder ran in production.');
        } catch (RuntimeException $e) {
            $this->assertStringContainsString('refuses to run in production', $e->getMessage());
        } finally {
            $this->app['env'] = 'testing';
        }

        $this->assertSame(0, User::where('email', 'demo@georag.dev')->count());
    }

    public function test_still_seeds_the_demo_admin_outside_production(): void
    {
        Project::factory()->create(['project_id' => '019d74a1-fba8-7165-9ae6-a5bf93eef97d']);

        $this->seed(DemoUserSeeder::class);

        $user = User::where('email', 'demo@georag.dev')->first();

        $this->assertNotNull($user);
        $this->assertTrue($user->is_admin);
    }
}
