<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        Schema::create('silver.exports', function (Blueprint $table) {
            $table->uuid('export_id')->primary();
            $table->foreignUuid('project_id')->references('project_id')->on('silver.projects')->cascadeOnDelete();
            $table->string('export_type', 20);
            $table->string('status', 20)->default('pending');
            $table->string('format', 20)->nullable();
            $table->jsonb('filters')->default(DB::raw("'{}'::jsonb"));
            $table->integer('file_count')->nullable();
            $table->bigInteger('total_size_bytes')->nullable();
            $table->string('minio_path', 500)->nullable();
            $table->string('download_url', 1000)->nullable();
            $table->timestampTz('download_url_expires_at')->nullable();
            $table->text('error_message')->nullable();
            $table->timestampTz('completed_at')->nullable();
            $table->timestampsTz();
        });

        DB::statement('CREATE INDEX idx_exports_project ON silver.exports (project_id)');
        DB::statement('CREATE INDEX idx_exports_status ON silver.exports (status)');
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::dropIfExists('silver.exports');
    }
};
