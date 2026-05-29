<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Persist chat conversations + messages server-side.
 *
 * Why:
 *   - Until now, Chat.tsx stored the thread list in localStorage. That means
 *     clearing browser storage wipes history and a user logging in from a
 *     different device sees a blank slate. For an NI 43-101 workflow where
 *     the conversation is part of the audit trail, that's not acceptable.
 *
 *   - query_audit_log already records individual queries for compliance;
 *     this table layers "conversation grouping" on top so the UI can
 *     reconstruct threaded views without replaying every query in the DB.
 *
 * Design:
 *   - UUID primary key so conversation_id is safe to expose in URLs later.
 *   - Scoped to user_id (ON DELETE CASCADE) — conversations aren't shared.
 *   - Messages live in a separate table so we can paginate and so the
 *     JSONB payload stays bounded. `metadata` carries citations, confidence,
 *     map_payload, viz_payload — the full GeoRAGResponse minus `text`,
 *     which lives in `content`.
 *   - No foreign key from messages → query_audit_log. Those are
 *     append-only audit rows written by the Horizon job; coupling would
 *     force us to wait for job completion before a user's own message
 *     could be persisted.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('
            CREATE TABLE IF NOT EXISTS chat_conversations (
                conversation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL DEFAULT \'New conversation\',
                project_id UUID NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ');

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_chat_conversations_user_updated '
            . 'ON chat_conversations (user_id, updated_at DESC)'
        );

        DB::statement('
            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id UUID NOT NULL REFERENCES chat_conversations(conversation_id) ON DELETE CASCADE,
                role VARCHAR(16) NOT NULL CHECK (role IN (\'user\', \'assistant\', \'system\')),
                content TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT \'{}\'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ');

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created '
            . 'ON chat_messages (conversation_id, created_at ASC)'
        );
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS chat_messages');
        DB::statement('DROP TABLE IF EXISTS chat_conversations');
    }
};
