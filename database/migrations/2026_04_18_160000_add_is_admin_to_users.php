<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Add is_admin boolean column to the users table.
     *
     * Defaults to false — only explicitly promoted users become admins.
     * This column gates write access to global resources such as vendor
     * profiles and column mappings. See AppServiceProvider for the
     * Gate::define('admin', ...) definition that reads this column.
     */
    public function up(): void
    {
        Schema::table('users', function (Blueprint $table) {
            $table->boolean('is_admin')->default(false)->after('remember_token');
        });
    }

    public function down(): void
    {
        Schema::table('users', function (Blueprint $table) {
            $table->dropColumn('is_admin');
        });
    }
};
