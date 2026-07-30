<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * SQLite mirror for the raw-SQL workspace foundation and project columns.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        Schema::create('workspaces', function (Blueprint $table): void {
            $table->uuid('workspace_id')->primary();
            $table->string('name');
            $table->string('slug')->unique();
            $table->unsignedBigInteger('data_version')->default(0);
            $table->timestamps();
        });

        DB::table('workspaces')->insert([
            'workspace_id' => 'a0000000-0000-0000-0000-000000000001',
            'name' => 'Default Workspace',
            'slug' => 'default',
            'data_version' => 0,
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        Schema::table('projects', function (Blueprint $table): void {
            $table->uuid('workspace_id')->nullable();
            $table->unsignedBigInteger('data_version')->default(0);
        });
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        Schema::table('projects', function (Blueprint $table): void {
            $table->dropColumn(['workspace_id', 'data_version']);
        });
        Schema::dropIfExists('workspaces');
    }
};
