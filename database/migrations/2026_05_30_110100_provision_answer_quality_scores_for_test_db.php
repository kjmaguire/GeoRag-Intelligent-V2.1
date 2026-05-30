<?php

// Test-DB parity sibling for 2026_05_30_110000.
// Parent migration handles SQLite driver check; this is a documented no-op.
use Illuminate\Database\Migrations\Migration;

return new class extends Migration
{
    public function up(): void {}

    public function down(): void {}
};
