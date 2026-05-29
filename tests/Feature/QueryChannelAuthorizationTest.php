<?php

namespace Tests\Feature;

use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Broadcasting\Broadcasters\Broadcaster;
use Illuminate\Contracts\Broadcasting\Broadcaster as BroadcasterContract;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use ReflectionClass;
use Tests\TestCase;

/**
 * A1 regression — the query.{queryId} private channel must be authorised
 * against the owner + the user's current access to the row's project_id.
 *
 * Each case calls POST /broadcasting/auth directly so we exercise the real
 * callback wired in routes/channels.php, not a stub.
 */
class QueryChannelAuthorizationTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');
    }

    private function grantProjectAccess(User $user, Project $project, string $role = 'member'): void
    {
        DB::table('project_user')->insert([
            'project_id' => $project->project_id,
            'user_id'    => $user->id,
            'role'       => $role,
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }

    private function seedQuery(User $owner, Project $project): QueryAuditLog
    {
        return QueryAuditLog::create([
            'user_id'    => $owner->id,
            'project_id' => $project->project_id,
            'query_id'   => (string) Str::uuid(),
            'query_text' => 'What is the average gold grade?',
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'qwen2.5:14b',
        ]);
    }

    /**
     * Invoke the channels.php callback directly rather than going through
     * the HTTP auth endpoint. Rationale: phpunit.xml pins
     * BROADCAST_CONNECTION=null, and the null driver's auth endpoint
     * always returns 200 regardless of callback return value — so
     * HTTP-level assertions can't distinguish "allowed" from "denied".
     * The callback is the real security gate; test it directly.
     *
     * Returns whatever the callback returned (typically true or false).
     */
    private function callChannelAuth(User $user, string $queryId): mixed
    {
        $broadcaster = app(BroadcasterContract::class);
        $refl = new ReflectionClass($broadcaster);
        $prop = null;
        while ($refl !== false) {
            if ($refl->hasProperty('channels')) {
                $prop = $refl->getProperty('channels');
                break;
            }
            $refl = $refl->getParentClass();
        }
        $this->assertNotNull($prop, 'Broadcaster has no `channels` property to inspect.');
        $prop->setAccessible(true);
        $channels = $prop->getValue($broadcaster);

        $callback = $channels['query.{queryId}'] ?? null;
        $this->assertNotNull($callback, 'query.{queryId} channel not registered.');

        return $callback($user, $queryId);
    }

    public function test_owner_with_project_access_is_authorised(): void
    {
        $owner = User::factory()->create();
        $project = Project::factory()->create();
        $this->grantProjectAccess($owner, $project, 'owner');

        $row = $this->seedQuery($owner, $project);

        $this->assertTrue($this->callChannelAuth($owner, $row->query_id));
    }

    public function test_non_owner_on_same_tenant_is_rejected(): void
    {
        $owner = User::factory()->create();
        $other = User::factory()->create();
        $project = Project::factory()->create();
        $this->grantProjectAccess($owner, $project, 'owner');
        $this->grantProjectAccess($other, $project, 'member');

        $row = $this->seedQuery($owner, $project);

        // user_id mismatch must return false even though $other is in project_user.
        $this->assertFalse($this->callChannelAuth($other, $row->query_id));
    }

    public function test_owner_who_lost_project_access_is_rejected(): void
    {
        $owner = User::factory()->create();
        $project = Project::factory()->create();
        $this->grantProjectAccess($owner, $project, 'owner');

        $row = $this->seedQuery($owner, $project);

        // Revoke access after the query was reserved.
        DB::table('project_user')
            ->where('user_id', $owner->id)
            ->where('project_id', $project->project_id)
            ->delete();

        $this->assertFalse($this->callChannelAuth($owner, $row->query_id));
    }

    public function test_invalid_uuid_shape_is_rejected(): void
    {
        $owner = User::factory()->create();

        $this->assertFalse($this->callChannelAuth($owner, 'not-a-uuid'));
    }

    public function test_missing_audit_row_is_rejected(): void
    {
        $owner = User::factory()->create();

        // Valid UUID shape but no corresponding QueryAuditLog row.
        $this->assertFalse($this->callChannelAuth($owner, (string) Str::uuid()));
    }
}
