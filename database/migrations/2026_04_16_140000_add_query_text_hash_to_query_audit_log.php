<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

/**
 * PII-at-rest hardening (see A4 fix).
 *
 * `query_text` is encrypted at rest via Laravel's `encrypted` cast so DB
 * dumps do not leak JV partner names, unannounced assays, or acquisition
 * targets. Encrypted ciphertext is non-deterministic, which breaks the
 * analytics "top queries" aggregation (GROUP BY LOWER(query_text)).
 *
 * This column stores a SHA-256 hash of the normalised query so the
 * analytics layer can still group semantically-identical queries without
 * decrypting every audit row in the reporting window.
 *
 * Hashing is deterministic using APP_KEY as an HMAC salt — same inputs
 * produce the same hash per install, but the hashes are not comparable
 * across tenants/installs. See QueryAuditLog::setQueryTextAttribute().
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::table('query_audit_log', function (Blueprint $table) {
            $table->string('query_text_hash', 64)->nullable()->index();
        });
    }

    public function down(): void
    {
        Schema::table('query_audit_log', function (Blueprint $table) {
            $table->dropIndex(['query_text_hash']);
            $table->dropColumn('query_text_hash');
        });
    }
};
