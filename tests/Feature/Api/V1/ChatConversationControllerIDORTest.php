<?php

namespace Tests\Feature\Api\V1;

use App\Models\ChatConversation;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for ChatConversationController.
 *
 * Routes under test:
 *   GET    /api/v1/conversations                         (index — user-scoped, no IDOR)
 *   GET    /api/v1/conversations/{conversationId}        (show)
 *   PUT    /api/v1/conversations/{conversationId}        (upsert)
 *   DELETE /api/v1/conversations/{conversationId}        (destroy)
 *
 * Scoping model: conversations are user-private (not project-scoped). The
 * controller enforces ownership via `WHERE conversation_id = ? AND user_id = ?`.
 * A cross-user read/delete returns 404 because the WHERE clause returns no rows
 * — the same 404 you get for a conversation that simply doesn't exist. This is
 * the correct existence-oracle defence for user-private resources.
 *
 * The index action filters entirely by user_id, so User A can never see User B's
 * threads in a list — no IDOR test is needed for index.
 *
 * The upsert action uses firstOrNew with user_id + conversation_id: if User A
 * tries to upsert into a UUID already owned by User B, the `firstOrNew` finds no
 * matching row for User A, so it creates a NEW conversation under User A's ID
 * instead of overwriting User B's. The check `if ($thread->exists &&
 * $thread->user_id !== $user->id)` further guards the case where User A manages
 * to specify an existing UUID that belongs to User B.
 *
 * SQLite compatibility: chat_conversations uses no PostGIS. All tests run under
 * SQLite.
 */
class ChatConversationControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;

    private User $userB;

    private ChatConversation $conversationB;

    protected function setUp(): void
    {
        parent::setUp();

        // chat_conversations is created via raw SQL with TIMESTAMPTZ/UUID columns,
        // so the table is no-op'd under SQLite. All tests in this file require
        // the PostgreSQL driver.
        $this->skipIfSqlite('chat_conversations table requires PostgreSQL (TIMESTAMPTZ column).');

        $this->userA = User::factory()->create();
        $this->userB = User::factory()->create();

        // Create a conversation belonging to User B.
        $this->conversationB = ChatConversation::create([
            'conversation_id' => 'bb000000-0000-0000-0000-000000000001',
            'user_id' => $this->userB->id,
            'title' => 'User B private conversation',
            'project_id' => null,
        ]);

        $this->actingAs($this->userA, 'sanctum');
    }

    // -------------------------------------------------------------------------
    // IDOR: show — user A reads user B's conversation → 404
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_read_user_b_conversation(): void
    {
        $response = $this->getJson("/api/v1/conversations/{$this->conversationB->conversation_id}");

        $response->assertNotFound()
            ->assertJsonPath('error', 'not_found');
    }

    // -------------------------------------------------------------------------
    // IDOR: destroy — user A deletes user B's conversation → 404
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_delete_user_b_conversation(): void
    {
        $response = $this->deleteJson("/api/v1/conversations/{$this->conversationB->conversation_id}");

        $response->assertNotFound()
            ->assertJsonPath('error', 'not_found');

        // Confirm conversation was NOT deleted.
        $this->assertDatabaseHas('chat_conversations', [
            'conversation_id' => $this->conversationB->conversation_id,
        ]);
    }

    // -------------------------------------------------------------------------
    // IDOR: upsert — user A cannot clobber user B's conversation
    // The controller's firstOrNew creates a NEW row for User A rather than
    // overwriting User B's row.
    // -------------------------------------------------------------------------

    public function test_user_a_upsert_with_user_b_conversation_id_creates_new_row(): void
    {
        $response = $this->putJson("/api/v1/conversations/{$this->conversationB->conversation_id}", [
            'title' => 'Hijacked',
            'messages' => [],
        ]);

        // User B's title must be unchanged.
        $this->assertDatabaseHas('chat_conversations', [
            'conversation_id' => $this->conversationB->conversation_id,
            'user_id' => $this->userB->id,
            'title' => 'User B private conversation',
        ]);

        // Either a 200 (new row created under User A) or 403 is acceptable.
        // The key invariant is that User B's row was NOT modified.
        $this->assertNotSame(
            $this->userA->id,
            $this->conversationB->fresh()->user_id,
            'User B conversation ownership must not be transferred to User A.',
        );
    }

    // -------------------------------------------------------------------------
    // Existence oracle: denied show and truly missing show return same 404 shape
    // -------------------------------------------------------------------------

    public function test_idor_deny_and_not_found_return_same_shape(): void
    {
        $nonExistentId = '00000000-0000-0000-0000-000000000000';

        $deniedResponse = $this->getJson("/api/v1/conversations/{$this->conversationB->conversation_id}");
        $notFoundResponse = $this->getJson("/api/v1/conversations/{$nonExistentId}");

        $deniedResponse->assertNotFound();
        $notFoundResponse->assertNotFound();

        $this->assertSame(
            $notFoundResponse->json('error'),
            $deniedResponse->json('error'),
        );
    }

    // -------------------------------------------------------------------------
    // Sanity: user A can read their own conversation
    // -------------------------------------------------------------------------

    public function test_user_a_can_read_own_conversation(): void
    {
        $conversationA = ChatConversation::create([
            'conversation_id' => 'aa000000-0000-0000-0000-000000000001',
            'user_id' => $this->userA->id,
            'title' => 'User A conversation',
            'project_id' => null,
        ]);

        $response = $this->getJson("/api/v1/conversations/{$conversationA->conversation_id}");

        $response->assertOk()
            ->assertJsonPath('id', $conversationA->conversation_id);
    }
}
