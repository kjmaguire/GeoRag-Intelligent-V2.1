<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * SQLite mirror for bronze.source_files, whose production table is raw SQL.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        Schema::create('source_files', function (Blueprint $table): void {
            $table->uuid('id')->primary();
            $table->uuid('workspace_id');
            $table->text('seaweedfs_key')->unique();
            $table->text('original_filename');
            $table->text('file_sha256');
            $table->unsignedBigInteger('file_size_bytes')->nullable();
            $table->text('mime_type')->nullable();
            $table->text('source_type');
            $table->text('data_type')->nullable();
            $table->uuid('campaign_id')->nullable();
            $table->string('ingested_by')->nullable();
            $table->timestamp('ingested_at');
            $table->unique(['workspace_id', 'file_sha256']);
        });
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            Schema::dropIfExists('source_files');
        }
    }
};
