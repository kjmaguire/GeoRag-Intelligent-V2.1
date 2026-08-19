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
 * Ingest-quality coverage — now asserted against the merged reports surface.
 *
 * The "trust moment" page (/imports/quality, Foundry/IngestQuality) was
 * merged into /reports on 2026-08-18; the controller behind it is gone and
 * the URL redirects. Every assertion below survived the merge unchanged in
 * substance — only the route, the component and the prop paths moved:
 *
 *     /imports/quality  →  /reports
 *     files.0.name      →  reports.0.title
 *     files.0.rows      →  reports.0.passages
 *     files.0.accepted  →  reports.0.embedded
 *     files.0.format    →  reports.0.is_scanned
 *     totals.*          →  quality.totals.*
 *
 * The original coverage is what matters and it is all still here: the
 * 2026-08-17 rebuild off the dead §04p dual-write stack
 * (silver.document_ingestion_quality, silver.low_confidence_page_reviews,
 * which have no writer on the live path), the fail-closed-RLS regression on
 * silver.document_passages, and status being derived from embedded/passages
 * rather than the NI 43-101 structural-coverage score.
 *
 * Kept in this file rather than folded into ReportControllerTest so the
 * quality contract stays greppable by the name it was written under.
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

    public function test_reports_surface_404s_for_an_outsider(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertStatus(404);
    }

    public function test_empty_project_shows_empty_state(): void
    {
        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/Reports')
                    ->where('empty', true),
            );
    }

    public function test_document_list_reflects_reports_with_no_document_ingestion_quality_row(): void
    {
        $this->insertReport('NI 43-101 Madsen PFS', passages: 10, embedded: 8, qualityPct: 0.85);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('empty', false)
                    ->where('reports.0.title', 'NI 43-101 Madsen PFS')
                    ->where('reports.0.is_scanned', false)
                    ->where('reports.0.passages', 10)
                    ->where('reports.0.embedded', 8)
                    // 8/10 embedded — partial, not full — even though
                    // parse_quality_pct (0.85) would have scored "ok"
                    // under the old regex-coverage gate.
                    ->where('reports.0.status', 'warn'),
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
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('reports.0.status', 'ok'),
            );
    }

    public function test_scanned_report_shows_scanned_format(): void
    {
        $this->insertReport('Scanned Drill Log', passages: 5, embedded: 5, qualityPct: 0.2, isScanned: true);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('reports.0.is_scanned', true)
                    ->where('reports.0.status', 'ok'),
            );
    }

    public function test_report_with_zero_embedded_passages_shows_error_status(): void
    {
        $this->insertReport('Failed Embed Report', passages: 12, embedded: 0);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('reports.0.passages', 12)
                    ->where('reports.0.embedded', 0)
                    ->where('reports.0.status', 'error'),
            );
    }

    public function test_report_with_zero_passages_shows_unassessed_status(): void
    {
        $this->insertReport('Not Yet Parsed', passages: 0, embedded: 0);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('reports.0.passages', 0)
                    ->where('reports.0.status', 'unassessed'),
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
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('reports.0.passages', 500)
                    ->where('reports.0.embedded', 480),
            );
    }

    public function test_totals_derived_from_review_queue_not_dead_low_confidence_table(): void
    {
        $this->insertReport('Report A', passages: 20, embedded: 20);
        $this->insertReviewQueueRow(lifecycle: 'pending', decisionKind: null);
        $this->insertReviewQueueRow(lifecycle: 'pending', decisionKind: null);
        $this->insertReviewQueueRow(lifecycle: 'committed', decisionKind: 'approve_as_parsed');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    // 2 pending rows are "flagged"; the committed one is not.
                    ->where('quality.totals.flagged', 2)
                    ->where('quality.totals.awaiting_ocr', 2)
                    ->where('quality.totals.rejected', 0)
                    // accepted = 20 passages - 2 flagged - 0 rejected
                    ->where('quality.totals.accepted', 18),
            );
    }

    public function test_rejected_review_queue_rows_counted_separately(): void
    {
        $this->insertReport('Report A', passages: 10, embedded: 10);
        $this->insertReviewQueueRow(lifecycle: 'decided', decisionKind: 'reject');

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/reports")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('quality.totals.rejected', 1)
                    // decided+reject is not 'pending', so it doesn't count as
                    // awaiting_ocr — it's already been decided.
                    ->where('quality.totals.awaiting_ocr', 0),
            );
    }
}
