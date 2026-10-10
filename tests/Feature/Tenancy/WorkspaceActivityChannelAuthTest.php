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
 * workspace.{workspaceId}.activity — who may subscribe to a workspace's feed.
 *
 * Background — 2026-06-02 audit pass 5+ caught the original gate
 * (`return $user->projects()->exists()`) admitting any authenticated user with
 * ANY project to ANY workspace's activity feed: tenant A could subscribe to
 * tenant B's channel just by knowing the workspace UUID. The fix scopes the
 * check to `silver.projects.workspace_id` matching the channel parameter.
 *
 * This file used to pin that with regular expressions over the SOURCE TEXT of
 * routes/channels.php (`/silver\.projects\.workspace_id/`, `/\$user === null/`).
 * Those pass for a callback that negates exists(), reads the wrong variable, or
 * has the right words in a comment. It now calls the registered closure with
 * real users and real rows and asserts the answer, the way
 * QueryChannelAuthorizationTest does.
 */
class WorkspaceActivityChannelAuthTest extends TestCase
{
    use CallsBroadcastChannels;
    use RefreshDatabase;

    private const CHANNEL = 'workspace.{workspaceId}.activity';

    private string $workspaceA;

    private string $workspaceB;

    private User $inA;

    private User $inB;

    private User $nowhere;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceA = (string) Str::uuid();
        $this->workspaceB = (string) Str::uuid();

        $this->inA = $this->userWithProjectIn($this->workspaceA);
        $this->inB = $this->userWithProjectIn($this->workspaceB);
        $this->nowhere = User::factory()->create();
    }

    private function userWithProjectIn(string $workspaceId): User
    {
        $project = Project::factory()->create();
        DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => $workspaceId]);

        $user = User::factory()->create();
        $user->projects()->attach($project->project_id, ['role' => 'member']);

        return $user;
    }

    public function test_a_member_of_a_project_in_the_workspace_may_subscribe(): void
    {
        $this->assertTrue($this->callChannel(self::CHANNEL, $this->inA, $this->workspaceA));
        $this->assertTrue($this->callChannel(self::CHANNEL, $this->inB, $this->workspaceB));
    }

    public function test_a_member_of_another_tenants_workspace_may_not(): void
    {
        // The cross-tenant leak itself: A holds a project, just not in B.
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inA, $this->workspaceB));
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inB, $this->workspaceA));
    }

    public function test_a_user_with_no_projects_may_not_subscribe_to_anything(): void
    {
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->nowhere, $this->workspaceA));
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->nowhere, $this->workspaceB));
    }

    public function test_an_unauthenticated_subscriber_is_refused(): void
    {
        $this->assertFalse($this->callChannel(self::CHANNEL, null, $this->workspaceA));
    }

    public function test_a_workspace_with_no_projects_is_refused_for_everyone(): void
    {
        $empty = (string) Str::uuid();

        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inA, $empty));
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inB, $empty));
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function malformedWorkspaceIds(): array
    {
        return [
            'not a uuid' => ['not-a-uuid'],
            'empty' => [''],
            'sql fragment' => ["x' OR '1'='1"],
            'wildcard' => ['%'],
            'uuid with a suffix' => ['5ec20000-0000-4000-8000-00000000000a-extra'],
            'uuid missing a group' => ['5ec20000-0000-4000-8000'],
            'braced uuid' => ['{5ec20000-0000-4000-8000-00000000000a}'],
        ];
    }

    #[DataProvider('malformedWorkspaceIds')]
    public function test_a_malformed_workspace_id_is_refused_without_touching_the_database(string $malformed): void
    {
        $queries = 0;
        DB::listen(function () use (&$queries): void {
            $queries++;
        });

        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inA, $malformed));
        $this->assertSame(0, $queries, 'the shape check must run before any lookup');
    }

    public function test_access_follows_membership_at_subscribe_time(): void
    {
        $this->assertTrue($this->callChannel(self::CHANNEL, $this->inA, $this->workspaceA));

        DB::table('project_user')->where('user_id', $this->inA->id)->delete();

        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inA, $this->workspaceA));
    }

    public function test_a_user_in_two_workspaces_gets_exactly_those_two(): void
    {
        $project = Project::factory()->create();
        DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => $this->workspaceB]);
        $this->inA->projects()->attach($project->project_id, ['role' => 'viewer']);

        $this->assertTrue($this->callChannel(self::CHANNEL, $this->inA, $this->workspaceA));
        $this->assertTrue($this->callChannel(self::CHANNEL, $this->inA, $this->workspaceB));
        $this->assertFalse($this->callChannel(self::CHANNEL, $this->inA, (string) Str::uuid()));
    }
}
