<?php

declare(strict_types=1);

namespace Tests\Concerns;

use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * chat_conversations / chat_messages are created by a raw-SQL migration
 * (UUID + JSONB + TIMESTAMPTZ) that the SQLite shim in Tests\TestCase
 * no-ops, so on the fast suite the tables simply do not exist and every
 * chat-persistence path went untested — which is how a query selecting a
 * column the table does not have (`id`, CHAT-2) shipped.
 *
 * This builds the same columns with the Schema builder when running on
 * SQLite. On Postgres (phpunit.pgsql.xml) the real migrations own them.
 */
trait CreatesChatTablesOnSqlite
{
    protected function createChatTablesIfMissing(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        if (! Schema::hasTable('chat_conversations')) {
            Schema::create('chat_conversations', function (Blueprint $table): void {
                $table->uuid('conversation_id')->primary();
                $table->unsignedBigInteger('user_id');
                $table->string('title')->default('New conversation');
                $table->uuid('project_id')->nullable();
                $table->text('state_json')->nullable();
                $table->timestamps();
            });
        }

        if (! Schema::hasTable('chat_messages')) {
            Schema::create('chat_messages', function (Blueprint $table): void {
                $table->uuid('message_id')->primary();
                $table->uuid('conversation_id');
                $table->string('role', 16);
                $table->text('content');
                $table->json('metadata')->default('{}');
                $table->integer('position')->default(0);
                $table->timestamp('created_at')->nullable();
            });
        }
    }
}
