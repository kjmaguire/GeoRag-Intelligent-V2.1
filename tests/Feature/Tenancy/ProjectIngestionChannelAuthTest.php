<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\Concerns\CallsBroadcastChannels;
use Tests\TestCase;

/**
 * project.{projectId}.ingestion — document-ingestion stage transitions.
 *
 * Nothing under tests/ called this closure. It carries two promises:
 *
 *   1. only members of the project may subscribe (a revoked member may not)
 *   2. a malformed channel name is refused WITHOUT a database lookup.
 *      hasProjectAccess() compares against silver.projects.project_id, a uuid
 *      column, so a malformed id used to reach Postgres as `invalid input
 *      syntax for type uuid`; that QueryException is not the missing-pivot case
 *      the method swallows, so POST /broadcasting/auth answered an
 *      unauthorised subscribe with a 500 instead of a 403. The shape check is
 *      what prevents it, and "zero queries" is how a test can see that on any
 *      driver, including SQLite, which would not have thrown.
 */
class ProjectIngestionChannelAuthTest extends TestCase
{
    use CallsBroadcastChannels;
    use RefreshDatabase;

    private const CHANNEL = 'project.{projectId}.ingestion';

    private Project $project;

    private User $member;

    private User $outsider;

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::factory()->create();
        $this->member = User::factory()->create();
        $this->member->projects()->attach($this->project->project_id, ['role' => 'viewer']);

        // Belongs to a project, just not this one.
        $this->outsider = User::factory()->create();
        $other = Project::factory()->create();
        $this->outsider->projects()->attach($other->project_id, ['role' => 'owner']);
    }

    public function test_a_member_may_subscribe(): void
    {
        $this->assertTrue($this->callChannel(self::CHANNEL, $this->member, $this->project->project_id));
    }

    public function test_a_user_who_is_not_a_member_may_not(): void
    {
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->outsider, $this->project->project_id));
    }

    public function test_an_unauthenticated_subscriber_is_refused(): void
    {
        $this->assertFalse($this->callChannel(self::CHANNEL, null, $this->project->project_id));
    }

    public function test_a_well_formed_id_of_a_project_that_does_not_exist_is_refused(): void
    {
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->member, (string) Str::uuid()));
    }

    public function test_access_follows_membership_at_subscribe_time(): void
    {
        $this->assertTrue($this->callChannel(self::CHANNEL, $this->member, $this->project->project_id));

        DB::table('project_user')->where('user_id', $this->member->id)->delete();

        $this->assertFalse($this->callChannel(self::CHANNEL, $this->member, $this->project->project_id));
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function malformedProjectIds(): array
    {
        return [
            'not a uuid' => ['not-a-uuid'],
            'empty' => [''],
            'a hole id' => ['PLS-22-08'],
            'sql fragment' => ["x' OR '1'='1"],
            'uuid with a suffix' => ['5ec20000-0000-4000-8000-00000000000a-extra'],
            'uuid missing a group' => ['5ec20000-0000-4000-8000'],
            'integer' => ['12345'],
        ];
    }

    #[DataProvider('malformedProjectIds')]
    public function test_a_malformed_project_id_is_refused_without_a_database_lookup(string $malformed): void
    {
        $queries = [];
        DB::listen(function ($query) use (&$queries): void {
            $queries[] = $query->sql;
        });

        $this->assertFalse($this->callChannel(self::CHANNEL, $this->member, $malformed));
        $this->assertSame([], $queries, 'the shape check must run before any lookup');
    }

    public function test_a_malformed_id_never_throws_for_an_unauthenticated_subscriber_either(): void
    {
        $this->assertFalse($this->callChannel(self::CHANNEL, null, 'not-a-uuid'));
    }
}
