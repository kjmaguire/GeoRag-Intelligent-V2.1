<?php

namespace Tests\Feature;

use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Crypt;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * A4 regression — query_text and response_text are encrypted at rest via
 * Laravel's `encrypted` cast and a deterministic SHA-256 hash is recorded
 * in query_text_hash for analytics grouping.
 */
class QueryAuditPiiEncryptionTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');
    }

    public function test_query_text_is_ciphertext_in_db_but_plaintext_on_model(): void
    {
        $row = QueryAuditLog::create([
            'user_id'    => null,
            'project_id' => (string) Str::uuid(),
            'query_id'   => (string) Str::uuid(),
            'query_text' => 'Confidential JV: Gold Corp / Rio XYZ acquisition',
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'qwen2.5:14b',
        ]);

        // Raw DB column must be ciphertext, not the plaintext.
        $raw = DB::table('query_audit_log')
            ->where('audit_id', $row->audit_id)
            ->value('query_text');

        $this->assertNotSame('Confidential JV: Gold Corp / Rio XYZ acquisition', $raw);
        $this->assertNotFalse(Crypt::decryptString($raw), 'Raw column should be Laravel-encrypted');

        // Reading via the model decrypts transparently.
        $fresh = QueryAuditLog::find($row->audit_id);
        $this->assertSame('Confidential JV: Gold Corp / Rio XYZ acquisition', $fresh->query_text);
    }

    public function test_query_text_hash_is_deterministic_and_populated(): void
    {
        $text = 'What is the average gold grade?';

        $a = QueryAuditLog::create([
            'user_id'    => null,
            'project_id' => (string) Str::uuid(),
            'query_id'   => (string) Str::uuid(),
            'query_text' => $text,
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'qwen2.5:14b',
        ]);

        $b = QueryAuditLog::create([
            'user_id'    => null,
            'project_id' => (string) Str::uuid(),
            'query_id'   => (string) Str::uuid(),
            // Whitespace / case must normalise to the same hash.
            'query_text' => '  WHAT is the average gold grade?  ',
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'qwen2.5:14b',
        ]);

        $this->assertNotNull($a->query_text_hash);
        $this->assertSame(64, strlen($a->query_text_hash));
        $this->assertSame($a->query_text_hash, $b->query_text_hash);

        // A different query produces a different hash.
        $c = QueryAuditLog::create([
            'user_id'    => null,
            'project_id' => (string) Str::uuid(),
            'query_id'   => (string) Str::uuid(),
            'query_text' => 'Show me lithology for DH-001',
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'qwen2.5:14b',
        ]);
        $this->assertNotSame($a->query_text_hash, $c->query_text_hash);
    }

    public function test_top_queries_aggregation_uses_hash_and_returns_plaintext(): void
    {
        $user = User::factory()->create();
        $project = Project::factory()->create();
        DB::table('project_user')->insert([
            'project_id' => $project->project_id,
            'user_id'    => $user->id,
            'role'       => 'owner',
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        // Three copies of a common query and one singleton — top result should
        // be the common one, grouped across case differences.
        $common = 'What is the average gold grade?';
        foreach (['What is the average gold grade?', 'what is the average gold grade?', 'WHAT IS THE AVERAGE GOLD GRADE?'] as $variant) {
            QueryAuditLog::create([
                'user_id'    => $user->id,
                'project_id' => $project->project_id,
                'query_id'   => (string) Str::uuid(),
                'query_text' => $variant,
                'ip_address' => '127.0.0.1',
                'llm_model'  => 'qwen2.5:14b',
            ]);
        }

        QueryAuditLog::create([
            'user_id'    => $user->id,
            'project_id' => $project->project_id,
            'query_id'   => (string) Str::uuid(),
            'query_text' => 'Show me lithology',
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'qwen2.5:14b',
        ]);

        $this->actingAs($user);

        $response = $this->getJson('/api/v1/dashboard/projects/' . $project->slug . '/analytics');
        $response->assertOk();

        $top = $response->json('query_usage.top_queries');
        $this->assertIsArray($top);
        $this->assertNotEmpty($top);

        // The most common grouped row should have count = 3 and the
        // decrypted representative plaintext.
        $this->assertSame(3, (int) $top[0]['c']);
        $this->assertSame(mb_strtolower(trim($common)), $top[0]['q']);
    }
}
