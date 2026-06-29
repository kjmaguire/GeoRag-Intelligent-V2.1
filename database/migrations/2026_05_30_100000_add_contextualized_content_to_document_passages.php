<?php
use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            DB::statement('ALTER TABLE "silver.document_passages" ADD COLUMN contextualized_content TEXT NULL');
            return;
        }
        DB::statement('ALTER TABLE silver.document_passages ADD COLUMN IF NOT EXISTS contextualized_content TEXT NULL');
        DB::statement("COMMENT ON COLUMN silver.document_passages.contextualized_content IS 'Anthropic contextual retrieval: LLM-generated context header prepended to raw text for embedding. NULL = not yet enriched.'");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_document_passages_needs_enrichment ON silver.document_passages (passage_id) WHERE contextualized_content IS NULL AND embedding_id IS NULL');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return; // SQLite cannot drop columns portably
        }
        DB::statement('DROP INDEX IF EXISTS idx_document_passages_needs_enrichment');
        DB::statement('ALTER TABLE silver.document_passages DROP COLUMN IF EXISTS contextualized_content');
    }
};
