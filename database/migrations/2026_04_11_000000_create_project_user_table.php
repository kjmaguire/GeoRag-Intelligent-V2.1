<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

/**
 * Pivot table linking users to projects with a role.
 *
 * Roles:
 *   - owner   : full CRUD on the project + manage members
 *   - member  : read + query + export; no project deletion
 *   - viewer  : read-only (future use)
 *
 * The pivot lives in the public schema alongside users (not silver)
 * because it's an application-level concern, not a geological data table.
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::create('project_user', function (Blueprint $table) {
            $table->id();
            $table->foreignId('user_id')->constrained('users')->cascadeOnDelete();
            $table->foreignUuid('project_id');
            $table->string('role', 20)->default('member'); // owner | member | viewer
            $table->timestamps();

            $table->unique(['user_id', 'project_id']);
            $table->index('project_id');

            // FK to silver.projects — use raw because Schema::create
            // defaults to the public schema for the table but silver for the ref.
            $table->foreign('project_id')
                  ->references('project_id')
                  ->on('silver.projects')
                  ->cascadeOnDelete();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('project_user');
    }
};
