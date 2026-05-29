<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

/**
 * Audit log for every RAG query — NI 43-101 compliance traceability.
 *
 * Every query submitted through the API is logged with:
 *   - Who asked (user_id)
 *   - What was asked (query text)
 *   - Which project (project_id)
 *   - What was cited (citations JSON from FastAPI response)
 *   - Confidence score
 *   - Response time
 *
 * This table lives in the public schema (not silver) because it's an
 * application-level concern, not geological data.
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::create('query_audit_log', function (Blueprint $table) {
            $table->uuid('audit_id')->primary()->default(DB::raw('gen_random_uuid()'));
            $table->foreignId('user_id')->nullable()->constrained('users')->nullOnDelete();
            $table->uuid('project_id')->nullable();
            $table->string('query_id', 36)->nullable()->index();
            $table->text('query_text');
            $table->text('response_text')->nullable();
            $table->jsonb('citations')->nullable();
            $table->jsonb('sources_used')->nullable();
            $table->float('confidence')->nullable();
            $table->integer('response_time_ms')->nullable();
            $table->string('llm_model', 100)->nullable();
            $table->string('ip_address', 45)->nullable();
            $table->timestamps();

            $table->index('user_id');
            $table->index('project_id');
            $table->index('created_at');
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('query_audit_log');
    }
};
