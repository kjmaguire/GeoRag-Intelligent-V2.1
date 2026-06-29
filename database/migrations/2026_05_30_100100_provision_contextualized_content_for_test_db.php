<?php
// Test-DB parity sibling for 2026_05_30_100000_add_contextualized_content_to_document_passages.
// The parent migration handles SQLite via driver check; this sibling is a
// documented no-op kept for parity completeness.
use Illuminate\Database\Migrations\Migration;

return new class extends Migration
{
    public function up(): void {}

    public function down(): void {}
};
