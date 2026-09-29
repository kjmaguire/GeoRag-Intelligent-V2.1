<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Adds `public.chat_messages.position`, the message's 0-based place in its
 * thread (CHAT-2 / LAR-5).
 *
 * Every reader ordered messages by `created_at` alone, and that column
 * cannot order a thread: ChatConversationController::upsert() deletes and
 * re-inserts the whole thread per sync, and Eloquent writes timestamps at
 * one-second precision, so every message of a thread shares one
 * `created_at`. Ties then come back in physical order, which drifts from
 * insertion order once VACUUM reuses the space earlier syncs freed --
 * threads re-opened with turns interleaved wrongly, and the multi-turn
 * history sent to FastAPI could resolve "it" against the wrong turn.
 *
 * NOT NULL DEFAULT 0 so a writer that predates this column still inserts.
 * Existing rows are backfilled by (created_at, message_id) -- the best
 * order still recoverable for them; new syncs write the client's index.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('public', 'chat_messages')) {
            // Created by 2026_04_16_130000_create_chat_conversations_table.
            return;
        }

        DB::statement(
            'ALTER TABLE public.chat_messages
               ADD COLUMN IF NOT EXISTS position INTEGER NOT NULL DEFAULT 0',
        );

        DB::statement('
            UPDATE public.chat_messages AS m
               SET position = ordered.rn
              FROM (
                    SELECT message_id,
                           (ROW_NUMBER() OVER (
                                PARTITION BY conversation_id
                                ORDER BY created_at ASC, message_id ASC
                           ) - 1)::INTEGER AS rn
                      FROM public.chat_messages
                   ) AS ordered
             WHERE ordered.message_id = m.message_id
        ');

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_position
               ON public.chat_messages (conversation_id, position)',
        );

        DB::statement(
            "COMMENT ON COLUMN public.chat_messages.position IS
             '0-based order of the message within its thread. Written by ChatConversationController::upsert; every reader orders by it.'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS public.idx_chat_messages_conversation_position');
        DB::statement('ALTER TABLE public.chat_messages DROP COLUMN IF EXISTS position');
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present
               FROM information_schema.tables
              WHERE table_schema = ? AND table_name = ?',
            [$schema, $table],
        ) !== null;
    }
};
