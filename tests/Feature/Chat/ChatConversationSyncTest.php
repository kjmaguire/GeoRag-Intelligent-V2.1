<?php

declare(strict_types=1);

namespace Tests\Feature\Chat;

use App\Models\ChatConversation;
use App\Models\ChatMessage;
use App\Models\User;
use Illuminate\Database\UniqueConstraintViolationException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Illuminate\Testing\TestResponse;
use Tests\Concerns\CreatesChatTablesOnSqlite;
use Tests\TestCase;

/**
 * PUT /api/v1/conversations/{id} — the full-replace sync Chat.tsx calls
 * after every answer.
 */
final class ChatConversationSyncTest extends TestCase
{
    use CreatesChatTablesOnSqlite;
    use RefreshDatabase;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();
        $this->createChatTablesIfMissing();
        $this->user = User::factory()->create();
        $this->actingAs($this->user);
    }

    /**
     * @param list<array<string, mixed>> $messages
     */
    private function sync(string $id, array $messages): TestResponse
    {
        return $this->putJson("/api/v1/conversations/{$id}", [
            'title' => 'PLS grades',
            'messages' => $messages,
        ]);
    }

    public function test_messages_keep_the_clients_order(): void
    {
        // CHAT-2 / LAR-5: every message of a sync is inserted in the same
        // second, so created_at cannot order them; position does.
        $id = (string) Str::uuid();
        $texts = ['q1', 'a1', 'q2', 'a2', 'q3'];

        $this->sync($id, array_map(fn (string $t, int $i): array => [
            'role' => $i % 2 === 0 ? 'user' : 'assistant',
            'content' => $t,
        ], $texts, array_keys($texts)))->assertOk();

        $stored = ChatConversation::with('messages')->find($id)->messages;
        $this->assertSame($texts, $stored->pluck('content')->all());
        $this->assertSame([0, 1, 2, 3, 4], $stored->pluck('position')->all());
    }

    public function test_an_assistant_turn_that_failed_before_any_text_does_not_block_the_thread(): void
    {
        // CHAT-4: '' became null in middleware, `required` 422'd, and every
        // later sync of the thread was silently lost.
        $id = (string) Str::uuid();

        $this->sync($id, [
            ['role' => 'user', 'content' => 'q1'],
            ['role' => 'assistant', 'content' => '', 'metadata' => ['error' => 'Query timed out', 'error_code' => 'TIMEOUT']],
            ['role' => 'user', 'content' => 'q2'],
            ['role' => 'assistant', 'content' => 'a2'],
        ])->assertOk();

        $this->assertSame(4, ChatMessage::where('conversation_id', $id)->count());
        $this->assertSame('', ChatMessage::where('conversation_id', $id)->where('position', 1)->value('content'));
    }

    public function test_a_sync_may_not_empty_an_existing_thread(): void
    {
        // CHAT-3: "+ New" mid-answer made the late completed handler PUT an
        // empty transcript under the old id, erasing the whole thread.
        $id = (string) Str::uuid();
        $this->sync($id, [['role' => 'user', 'content' => 'q1'], ['role' => 'assistant', 'content' => 'a1']])->assertOk();

        $this->sync($id, [])->assertStatus(409);

        $this->assertSame(2, ChatMessage::where('conversation_id', $id)->count());
        $this->assertSame('PLS grades', ChatConversation::find($id)->title);
    }

    public function test_another_users_thread_id_is_forbidden_not_a_server_error(): void
    {
        // LAR-18: the 403 branch was unreachable; a colliding id hit the
        // primary key on INSERT and 500'd.
        $id = (string) Str::uuid();
        $this->sync($id, [['role' => 'user', 'content' => 'mine']])->assertOk();

        $this->actingAs(User::factory()->create());
        $this->sync($id, [['role' => 'user', 'content' => 'theirs']])->assertForbidden();

        $this->assertSame('mine', ChatMessage::where('conversation_id', $id)->value('content'));
    }

    public function test_the_sync_is_bounded(): void
    {
        $id = (string) Str::uuid();
        $tooMany = array_fill(0, 501, ['role' => 'user', 'content' => 'x']);

        $this->sync($id, $tooMany)->assertUnprocessable()->assertJsonValidationErrors(['messages']);
        $this->sync($id, [['role' => 'user', 'content' => str_repeat('x', 100_001)]])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['messages.0.content']);
    }

    public function test_a_messages_metadata_is_capped_at_32_kb(): void
    {
        $id = (string) Str::uuid();
        $huge = ['citations' => [str_repeat('x', 33_000)]];

        $this->sync($id, [['role' => 'assistant', 'content' => 'a', 'metadata' => $huge]])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['messages.0.metadata']);

        $this->assertNull(ChatConversation::find($id), 'a rejected sync must not create the thread');

        $ok = ['citations' => [str_repeat('x', 30_000)]];
        $this->sync($id, [['role' => 'assistant', 'content' => 'a', 'metadata' => $ok]])->assertOk();
        $this->assertSame($ok, ChatMessage::where('conversation_id', $id)->first()->metadata);
    }

    public function test_a_sync_of_500_messages_is_accepted_and_ordered(): void
    {
        $id = (string) Str::uuid();
        $messages = [];
        for ($i = 0; $i < 500; $i++) {
            $messages[] = ['role' => $i % 2 === 0 ? 'user' : 'assistant', 'content' => "m{$i}", 'metadata' => ['i' => $i]];
        }

        $this->sync($id, $messages)->assertOk();

        $stored = ChatConversation::with('messages')->find($id)->messages;
        $this->assertCount(500, $stored);
        $this->assertSame('m0', $stored->first()->content);
        $this->assertSame('m499', $stored->last()->content);
        $this->assertSame(499, $stored->last()->position);
    }

    public function test_a_row_created_by_a_concurrent_first_sync_is_synced_into_not_a_500(): void
    {
        // The loser of a concurrent first sync finds the winner's row
        // already there. The old SELECT ... FOR UPDATE locked nothing for a
        // row that did not exist yet, both inserted, and one PK-violated.
        // Create-if-absent (ON CONFLICT DO NOTHING) must now fall through to
        // the locked read and a normal full replace.
        $id = (string) Str::uuid();
        DB::table('chat_conversations')->insert([
            'conversation_id' => $id,
            'user_id' => $this->user->id,
            'title' => 'winner',
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $this->sync($id, [['role' => 'user', 'content' => 'loser sync']])->assertOk();

        $this->assertSame(1, ChatConversation::where('conversation_id', $id)->count());
        $this->assertSame('PLS grades', ChatConversation::find($id)->title);
        $this->assertSame(['loser sync'], ChatMessage::where('conversation_id', $id)->pluck('content')->all());
    }

    public function test_contention_that_survives_the_retries_is_a_409_not_a_500(): void
    {
        $id = (string) Str::uuid();
        DB::connection()->beforeExecuting(function (string $query, array $bindings, $connection): void {
            if (str_contains($query, 'chat_messages') && str_starts_with(strtolower(ltrim($query)), 'insert')) {
                $pdo = new \PDOException('duplicate key value violates unique constraint');
                $pdo->errorInfo = ['23505', 7, 'duplicate key'];

                throw new UniqueConstraintViolationException($connection->getName(), $query, $bindings, $pdo);
            }
        });

        $this->sync($id, [['role' => 'user', 'content' => 'q']])
            ->assertStatus(409)
            ->assertJsonPath('error', 'sync_conflict');
    }
}
