<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Adds `public.chat_conversations.state_json`, the backing store for agentic
 * retrieval's multi-turn conversation state.
 *
 * This column has no DDL anywhere in the repository — not in a migration, not
 * in `database/raw/`. It exists on the local cluster because it was added by
 * hand, and nowhere else. `src/fastapi/app/services/conversation_state_store.py`
 * reads and writes it on every agentic turn, and `AGENTIC_RETRIEVAL_V2_ENABLED`
 * is `true` on fastapi-cc.
 *
 * Nothing crashes, which is why this went unnoticed. Both helpers wrap the
 * query in `try/except`, log at WARNING and return None:
 *
 *   read_conversation_state:  "failed for conversation_id=%s (non-fatal)"
 *   update_conversation_state: same shape
 *
 * So on Azure the state simply never persists. Every turn starts cold: no
 * entity focus carried forward, no last_query_class, no multi-turn resolution.
 * The feature reports as enabled and silently degrades to single-turn
 * behaviour. There is no error to find in the logs unless someone runs an
 * agentic chat and goes looking for the warning.
 *
 * jsonb, nullable, no default. NULL is a meaningful value the reader already
 * handles — `state_json IS NULL` is its documented "fresh conversation, hasn't
 * exercised agentic retrieval yet" path — so defaulting to '{}' would replace
 * a state the code understands with one it would have to validate and reject.
 *
 * Idempotent: ADD COLUMN IF NOT EXISTS, so the local cluster that already has
 * the column is a no-op.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->tableExists('public', 'chat_conversations')) {
            // Created by 2026_04_16_130000_create_chat_conversations_table.
            // Absent means the chain is broken upstream.
            return;
        }

        DB::statement(
            'ALTER TABLE public.chat_conversations
               ADD COLUMN IF NOT EXISTS state_json jsonb NULL',
        );

        DB::statement(
            "COMMENT ON COLUMN public.chat_conversations.state_json IS
             'Serialised ConversationState for agentic retrieval v2. NULL = fresh conversation. Read/written by src/fastapi/app/services/conversation_state_store.py.'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Dropping discards accumulated conversation state, and the reader
        // treats a missing column and a NULL value identically (both fall into
        // the non-fatal except path), so there is no behavioural reason to
        // reverse this. Left as a no-op rather than a data-losing rollback.
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
