<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * IngestQualityController — the "trust moment" surface.
 *
 * Regression coverage for the 2026-08-17 rebuild: this page previously
 * read exclusively from the §04p dual-write OCR quality stack
 * (silver.document_ingestion_quality, silver.low_confidence_page_reviews),
 * which has no writer on the live path (P04P_DUAL_WRITE_ENABLED=false) —
 * so it always rendered "empty" for every real project. Rebuilt to read
 * silver.reports/document_passages (live per-file data) and
 * silver.review_queue (the real, live OCR review-routing table).
 *
 * Postgres-only. Run with:
 *   php artisan test -c phpunit.pgsql.xml --filter=IngestQualityControllerTest
 */
final class IngestQualityControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
        $slug = 'iq-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Ingest Quality Test Workspace', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);
    }

    private function insertReport(string $title, int $passages, int $embedded, float $qualityPct = 0.82, bool $isScanned = false): string
    {
        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'title' => $title,
            'parser_used' => 'fitz',
            'parse_quality_pct' => $qualityPct,
            'is_scanned' => $isScanned,
            'version' => 1,
            'qp_name' => '{}',
        ]);

        for ($i = 0; $i < $passages; $i++) {
            DB::table('silver.document_passages')->insert([
                'passage_id' => (string) Str::uuid(),
                'document_id' => $reportId,
                'workspace_id' => $this->workspaceId,
                'revision_number' => 1,
                'text' => "passage {$i} of {$title}",
                'text_hash' => str_pad((string) $i, 64, '0', STR_PAD_LEFT),
                'ordinal' => $i,
                'embedding_id' => $i < $embedded ? "qdrant:abc:{$i}" : null,
            ]);
        }

        return $reportId;
    }

    private function insertReviewQueueRow(string $lifecycle, ?string $decisionKind, string $routingDecision = 'review_required'): void
    {
        DB::table('silver.review_queue')->insert([
            'queue_id' => (string) Str::uuid(),
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'target_table' => 'silver.document_passages',
            'target_record_kind' => 'ocr_page',
            'bronze_uri' => 's3://bronze/reports/'.$this->project->project_id.'/test.pdf',
            'payload' => '{}',
            'confidence_per_field' => '{}',
            'confidence_record' => 0.4,
            'parser_version' => 'fitz-1.0',
            'routing_decision' => $routingDecision,
            'lifecycle' => $lifecycle,
            'decision_kind' => $decisionKind,
            'outlier_flags' => '[]',
        ]);
    }

    public function test_show_redirects_outsider_to_404(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertStatus(404);
    }

    public function test_empty_project_shows_empty_state(): void
    {
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/IngestQuality')
                    ->where('empty', true),
            );
    }

    public function test_files_list_reflects_reports_with_no_document_ingestion_quality_row(): void
    {
        $this->insertReport('NI 43-101 Madsen PFS', passages: 10, embedded: 8, qualityPct: 0.85);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('empty', false)
                    ->where('files.0.name', 'NI 43-101 Madsen PFS')
                    ->where('files.0.format', 'PDF')
                    ->where('files.0.rows', 10)
                    ->where('files.0.accepted', 8)
                    // 8/10 embedded — partial, not full — even though
                    // parse_quality_pct (0.85) would have scored "ok"
                    // under the old regex-coverage gate.
                    ->where('files.0.status', 'warn'),
            );
    }

    public function test_fully_embedded_report_shows_ok_status_regardless_of_quality_pct(): void
    {
        // Deliberately low parse_quality_pct (this document doesn't look
        // like an NI 43-101 report — e.g. a corporate presentation) but
        // every passage made it into the retrievable index. Status must
        // reflect that, not the NI 43-101 structural-coverage score.
        $this->insertReport('CORPORATE PRESENTATION', passages: 19, embedded: 19, qualityPct: 0.06);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('files.0.status', 'ok'),
            );
    }

    public function test_scanned_report_shows_scanned_format(): void
    {
        $this->insertReport('Scanned Drill Log', passages: 5, embedded: 5, qualityPct: 0.2, isScanned: true);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('files.0.format', 'SCANNED PDF')
                    ->where('files.0.status', 'ok'),
            );
    }

    public function test_report_with_zero_embedded_passages_shows_error_status(): void
    {
        $this->insertReport('Failed Embed Report', passages: 12, embedded: 0);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('files.0.rows', 12)
                    ->where('files.0.accepted', 0)
                    ->where('files.0.status', 'error'),
            );
    }

    public function test_report_with_zero_passages_shows_unassessed_status(): void
    {
        $this->insertReport('Not Yet Parsed', passages: 0, embedded: 0);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('files.0.rows', 0)
                    ->where('files.0.status', 'unassessed'),
            );
    }

    public function test_passages_reflect_across_fail_closed_rls_on_document_passages(): void
    {
        // Regression for the 2026-08-17 bug: silver.document_passages
        // carries fail-closed RLS (no NULL-GUC fallback, unlike
        // silver.reports). A controller that queries it without binding
        // app.workspace_id gets zero rows back for every project,
        // regardless of how much real data exists. A large passage count
        // makes an accidental fallback to "0 rows visible" obvious.
        $this->insertReport('Large Report', passages: 500, embedded: 480);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('files.0.rows', 500)
                    ->where('files.0.accepted', 480),
            );
    }

    public function test_totals_derived_from_review_queue_not_dead_low_confidence_table(): void
    {
        $this->insertReport('Report A', passages: 20, embedded: 20);
        $this->insertReviewQueueRow(lifecycle: 'pending', decisionKind: null);
        $this->insertReviewQueueRow(lifecycle: 'pending', decisionKind: null);
        $this->insertReviewQueueRow(lifecycle: 'committed', decisionKind: 'approve_as_parsed');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    // 2 pending rows are "flagged"; the committed one is not.
                    ->where('totals.flagged', 2)
                    ->where('totals.awaiting_ocr', 2)
                    ->where('totals.rejected', 0)
                    // accepted = 20 passages - 2 flagged - 0 rejected
                    ->where('totals.accepted', 18),
            );
    }

    public function test_rejected_review_queue_rows_counted_separately(): void
    {
        $this->insertReport('Report A', passages: 10, embedded: 10);
        $this->insertReviewQueueRow(lifecycle: 'decided', decisionKind: 'reject');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/imports/quality")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('totals.rejected', 1)
                    // decided+reject is not 'pending', so it doesn't count as
                    // awaiting_ocr — it's already been decided.
                    ->where('totals.awaiting_ocr', 0),
            );
    }
}
