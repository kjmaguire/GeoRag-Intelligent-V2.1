<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use App\Http\Controllers\Internal\AdminSurfaceUpdatedBridgeController;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Str;
use ReflectionClassConstant;
use Tests\Concerns\CallsBroadcastChannels;
use Tests\TestCase;

/**
 * admin.* broadcast channels — admin Gate only.
 *
 * These channels carry workflow-run, ingest, support-cockpit, LLM-cost and
 * audit-explorer updates for the whole platform. All but one are registered
 * with the same `static fn ($user) => (bool) ($user->is_admin ?? false)`
 * closure, and `admin.target-run.{run_id}` adds a UUID guard. None of it was
 * called by a test, so a closure that returned true (or a new channel
 * registered with a permissive one) would have broadcast one tenant's job
 * metadata to every logged-in user.
 *
 * The channel list is read from the broadcaster, not typed here: a new
 * `Broadcast::channel('admin.something', ...)` is covered the day it is added.
 * A floor on the count stops the loops being vacuous if the registrations ever
 * move.
 */
class AdminChannelAuthTest extends TestCase
{
    use CallsBroadcastChannels;
    use RefreshDatabase;

    private const RUN_ID = '5ec20000-0000-4000-8000-0000000000aa';

    private const TARGET_RUN = 'admin.target-run.{run_id}';

    /**
     * @return list<string> every registered `admin.*` channel pattern
     */
    private function adminChannels(): array
    {
        return array_values(array_filter(
            array_keys($this->registeredChannels()),
            static fn (string $pattern): bool => str_starts_with($pattern, 'admin.'),
        ));
    }

    /**
     * The parameters a pattern needs: only admin.target-run.{run_id} has one.
     *
     * @return list<string>
     */
    private function parametersFor(string $pattern): array
    {
        return $pattern === self::TARGET_RUN ? [self::RUN_ID] : [];
    }

    public function test_the_loops_below_are_not_vacuous(): void
    {
        $this->assertGreaterThanOrEqual(
            20,
            count($this->adminChannels()),
            'routes/channels.php registers 23 admin.* channels (2026-10-10); if this fails the registrations moved '
            .'and the loops below are checking nothing.',
        );
        $this->assertContains(self::TARGET_RUN, $this->adminChannels());
    }

    public function test_every_admin_channel_admits_an_admin(): void
    {
        $admin = User::factory()->admin()->create();

        foreach ($this->adminChannels() as $pattern) {
            $this->assertTrue(
                $this->callChannel($pattern, $admin, ...$this->parametersFor($pattern)),
                "{$pattern} must admit an admin",
            );
        }
    }

    public function test_every_admin_channel_refuses_an_ordinary_user(): void
    {
        $member = User::factory()->create(['is_admin' => false]);

        foreach ($this->adminChannels() as $pattern) {
            $this->assertFalse(
                $this->callChannel($pattern, $member, ...$this->parametersFor($pattern)),
                "{$pattern} must refuse a non-admin",
            );
        }
    }

    public function test_every_admin_channel_refuses_an_unauthenticated_subscriber(): void
    {
        foreach ($this->adminChannels() as $pattern) {
            $this->assertFalse(
                $this->callChannel($pattern, null, ...$this->parametersFor($pattern)),
                "{$pattern} must refuse a subscriber with no user",
            );
        }
    }

    public function test_the_target_run_channel_needs_a_well_formed_run_id_even_for_an_admin(): void
    {
        $admin = User::factory()->admin()->create();

        $this->assertTrue($this->callChannel(self::TARGET_RUN, $admin, (string) Str::uuid()));

        foreach (['not-a-uuid', '', "x' OR '1'='1", '5ec20000-0000-4000-8000', 'target-run'] as $malformed) {
            $this->assertFalse(
                $this->callChannel(self::TARGET_RUN, $admin, $malformed),
                "malformed run id '{$malformed}' must be refused",
            );
        }
    }

    public function test_the_target_run_channel_refuses_a_non_admin_with_a_valid_run_id(): void
    {
        $member = User::factory()->create(['is_admin' => false]);

        $this->assertFalse($this->callChannel(self::TARGET_RUN, $member, self::RUN_ID));
    }

    public function test_owning_a_project_does_not_make_a_user_an_admin(): void
    {
        $owner = User::factory()->create(['is_admin' => false]);
        $project = Project::factory()->create();
        $owner->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->assertFalse($this->callChannel('admin.workflow-runs', $owner));
    }

    /**
     * AdminSurfaceUpdatedBridgeController::ALLOWED_SURFACES lists the surfaces
     * FastAPI workflows may broadcast to; its docblock says it "must match the
     * channel registrations in routes/channels.php". A surface with no channel
     * broadcasts into nothing, a channel with no surface can never receive
     * anything, and drift in either direction is silent.
     */
    public function test_the_bridge_allow_list_and_the_registered_channels_agree(): void
    {
        $surfaces = (new ReflectionClassConstant(
            AdminSurfaceUpdatedBridgeController::class,
            'ALLOWED_SURFACES',
        ))->getValue();
        $this->assertNotEmpty($surfaces);

        // 'admin.target-run.{run_id}' serves the 'target-run' surface.
        $registeredSurfaces = array_map(
            static fn (string $pattern): string => explode('.', substr($pattern, strlen('admin.')))[0],
            $this->adminChannels(),
        );

        $this->assertEqualsCanonicalizing(
            $surfaces,
            array_values(array_unique($registeredSurfaces)),
            'AdminSurfaceUpdatedBridgeController::ALLOWED_SURFACES and the admin.* channels in routes/channels.php disagree',
        );
    }
}
