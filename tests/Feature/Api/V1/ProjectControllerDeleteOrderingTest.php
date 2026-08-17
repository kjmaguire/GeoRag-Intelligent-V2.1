<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Regression for the 2026-08-17 project-deletion ordering bug.
 *
 * silver.answer_citation_items has
 * `CHECK (evidence_id IS NOT NULL OR passage_id IS NOT NULL)`
 * (answer_citation_items_has_target) alongside a `passage_id` FK to
 * silver.document_passages with ON DELETE SET NULL. Deleting
 * silver.reports before silver.answer_runs in ProjectController::destroy()
 * cascaded to document_passages, which SET NULL'd passage_id on any
 * legacy citation row that never had evidence_id populated (the
 * pre-evidence_id write path — see 2026_04_21_150000_create_answer_
 * citation_items's docblock) — leaving both target columns null and
 * violating the CHECK, which aborted the whole delete transaction.
 * Confirmed live on two real projects with chat history.
 *
 * silver.answer_runs is now deleted FIRST, cascading away its citation
 * rows before reports/document_passages ever gets a chance to SET NULL
 * into them. This test reproduces the exact failing shape: a citation
 * item bound only to a passage, no evidence_id.
 *
 * Postgres-only — the CHECK constraint + FK cascade interaction being
 * tested doesn't exist under the SQLite test-DB's flattened schema.
 */
final class ProjectControllerDeleteOrderingTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_destroy_succeeds_with_a_legacy_passage_only_citation(): void
    {
        $user = User::factory()->create();
        $workspaceId = (string) Str::uuid();
        $slug = 'del-order-'.substr($workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'Delete Ordering Test Workspace', $slug],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([
            $project->project_id => ['role' => 'owner'],
        ]);

        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $workspaceId,
            'project_id' => $project->project_id,
            'title' => 'Ordering Test Report',
            'parser_used' => 'fitz',
            'parse_quality_pct' => 0.9,
            'is_scanned' => false,
            'version' => 1,
            'qp_name' => '{}',
        ]);

        $passageId = (string) Str::uuid();
        DB::table('silver.document_passages')->insert([
            'passage_id' => $passageId,
            'document_id' => $reportId,
            'workspace_id' => $workspaceId,
            'revision_number' => 1,
            'text' => 'passage text',
            'text_hash' => str_pad('1', 64, '0', STR_PAD_LEFT),
            'ordinal' => 0,
        ]);

        $answerRunId = (string) Str::uuid();
        DB::table('silver.answer_runs')->insert([
            'answer_run_id' => $answerRunId,
            'workspace_id' => $workspaceId,
            'project_id' => $project->project_id,
            'query_text' => 'What is the resource estimate?',
            'query_class' => 'factual',
            'workspace_data_version_at_query' => 1,
        ]);

        // The exact failing shape: passage_id set, evidence_id NULL — a
        // legacy citation that predates the evidence_id write path.
        DB::table('silver.answer_citation_items')->insert([
            'answer_citation_item_id' => (string) Str::uuid(),
            'answer_run_id' => $answerRunId,
            'workspace_id' => $workspaceId,
            'evidence_id' => null,
            'passage_id' => $passageId,
            'marker_text' => '[NI43:1]',
            'source_store' => 'qdrant',
            'confidence' => 0.87,
        ]);

        $response = $this->actingAs($user)
            ->deleteJson("/api/v1/projects/{$project->project_id}");

        $response->assertNoContent();
        $this->assertDatabaseMissing('silver.projects', ['project_id' => $project->project_id]);
    }
}
