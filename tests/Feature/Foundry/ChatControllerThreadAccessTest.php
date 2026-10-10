<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\ChatConversation;
use App\Models\ChatMessage;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * GET /projects/{slug}/chat?thread= — which thread's messages the page may
 * load.
 *
 * `?thread=` used to be loaded by bare conversation_id: any member could read
 * any user's transcript by id (IDOR), and a non-UUID value 500'd on the uuid
 * cast. The message window was also the FIRST 200, so a long thread lost its
 * newest answers.
 *
 * Postgres-only (silver.projects slug + the public.chat_* raw-SQL tables):
 *   php artisan test -c phpunit.pgsql.xml --filter=ChatControllerThreadAccessTest
 */
final class ChatControllerThreadAccessTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->project = Project::factory()->create();
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);
    }

    private function url(string $query = ''): string
    {
        return "/projects/{$this->project->slug}/chat".$query;
    }

    /**
     * @param array<int, string> $contents
     */
    private function thread(User $owner, Project $project, array $contents): string
    {
        $id = (string) Str::uuid();
        ChatConversation::create([
            'conversation_id' => $id,
            'user_id' => $owner->id,
            'title' => 'Thread '.substr($id, 0, 4),
            'project_id' => $project->project_id,
        ]);
        foreach (array_values($contents) as $i => $text) {
            ChatMessage::create([
                'conversation_id' => $id,
                'role' => $i % 2 === 0 ? 'user' : 'assistant',
                'content' => $text,
                'metadata' => [],
                'position' => $i,
            ]);
        }

        return $id;
    }

    public function test_another_users_thread_id_returns_no_messages(): void
    {
        $other = User::factory()->create();
        $other->projects()->syncWithoutDetaching([$this->project->project_id => ['role' => 'viewer']]);
        $foreignId = $this->thread($other, $this->project, ['secret question', 'secret answer']);

        $this->actingAs($this->user)
            ->get($this->url('?thread='.$foreignId))
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->component('Foundry/Chat')
                ->where('active_thread_id', null)
                ->where('active_thread', null)
                ->has('messages', 0)
                ->has('threads', 0),
            );
    }

    public function test_a_thread_of_the_same_user_in_another_project_returns_no_messages(): void
    {
        $otherProject = Project::factory()->create();
        $this->user->projects()->syncWithoutDetaching([$otherProject->project_id => ['role' => 'owner']]);
        $crossProjectId = $this->thread($this->user, $otherProject, ['q', 'a']);

        $this->actingAs($this->user)
            ->get($this->url('?thread='.$crossProjectId))
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('active_thread_id', null)
                ->has('messages', 0),
            );
    }

    public function test_a_garbage_thread_id_does_not_500(): void
    {
        $this->thread($this->user, $this->project, ['mine']);

        foreach (['not-a-uuid', "'; DROP TABLE x;--", '123', str_repeat('a', 500)] as $garbage) {
            $this->actingAs($this->user)
                ->get($this->url('?thread='.urlencode($garbage)))
                ->assertOk()
                ->assertInertia(fn (AssertableInertia $page) => $page
                    ->where('active_thread_id', null)
                    ->has('messages', 0),
                );
        }

        // An array-valued `?thread[]=x` is garbage too.
        $this->actingAs($this->user)->get($this->url('?thread[]=x'))->assertOk();
    }

    public function test_the_users_own_thread_loads_by_id_and_the_newest_is_the_default(): void
    {
        $older = $this->thread($this->user, $this->project, ['old q', 'old a']);
        $newer = $this->thread($this->user, $this->project, ['new q', 'new a']);
        DB::table('public.chat_conversations')->where('conversation_id', $older)->update(['updated_at' => now()->subDay()]);

        $this->actingAs($this->user)
            ->get($this->url('?thread='.$older))
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('active_thread_id', $older)
                ->where('messages.0.content', 'old q'),
            );

        $this->actingAs($this->user)
            ->get($this->url())
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('active_thread_id', $newer)
                ->where('messages.0.content', 'new q'),
            );
    }

    public function test_a_long_thread_loads_whole_so_the_next_sync_cannot_drop_its_head(): void
    {
        // The page PUTs back what it loaded plus the new turn, and the sync
        // is a full replace. Loading only the newest 200 of these 230 erased
        // m0..m29 on the next answer.
        $texts = [];
        for ($i = 0; $i < 230; $i++) {
            $texts[] = "m{$i}";
        }
        $id = $this->thread($this->user, $this->project, $texts);

        $this->actingAs($this->user)
            ->get($this->url('?thread='.$id))
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->has('messages', 230)
                ->where('messages.0.content', 'm0')
                ->where('messages.229.content', 'm229'),
            );
    }
}
