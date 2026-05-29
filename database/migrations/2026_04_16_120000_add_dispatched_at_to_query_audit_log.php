<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Adds `dispatched_at` to query_audit_log.
 *
 * Motivation: the subscribe-ACK handshake in QueryController splits the
 * RAG start flow into two calls:
 *
 *   1. POST /api/v1/queries               → reserve (no job dispatch)
 *   2. POST /api/v1/queries/{id}/start    → dispatch job
 *
 * This column is the idempotency guard for step 2 — a row lock + NULL check
 * ensures concurrent `start` calls dispatch exactly one job. Existing rows
 * are backfilled to `created_at` so historical audit data doesn't look
 * like it's reserved-but-never-dispatched.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE query_audit_log '
            . 'ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMP(0) NULL'
        );
        DB::statement(
            'UPDATE query_audit_log SET dispatched_at = created_at WHERE dispatched_at IS NULL'
        );
    }

    public function down(): void
    {
        DB::statement('ALTER TABLE query_audit_log DROP COLUMN IF EXISTS dispatched_at');
    }
};
