<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * OverviewController — the project landing page.
 *
 * Regression coverage for the 2026-08-18 fix: the REPORTS KPI was built from
 * an unfiltered `DB::table('silver.reports')->count()` while every other count
 * on the page was scoped by project_id. With a single project that reads
 * correctly by coincidence; add a second project and both Overviews report the
 * same corpus size. It also fed $nextAction, whose "Connect your first data
 * source" branch is gated on $reportsCount === 0 — unreachable once ANY
 * project in the database has ingested a report.
 *
 * Postgres-only: silver.reports/silver.workspaces live in the pgsql test DB.
 *   php artisan test -c phpunit.pgsql.xml --filter=OverviewControllerTest
 */
final class OverviewControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Overview Test Workspace', 'overview-'.substr($this->workspaceId, 0, 8)],
        );

        // The ingest tile lists every bronze upload prefix for the project
        // (IngestionSnapshot::listUploads). Keep that off a real object
        // store for every test, not only the ones about the tile.
        Storage::fake('s3-bronze');
    }

    private function makeProject(): Project
    {
        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $project->project_id => ['role' => 'owner'],
        ]);

        return $project;
    }

    private function insertReport(Project $project, string $title, ?string $sourceObjectKey = null): void
    {
        DB::table('silver.reports')->insert([
            'report_id' => (string) Str::uuid(),
            'workspace_id' => $this->workspaceId,
            'project_id' => $project->project_id,
            'title' => $title,
            'page_count' => 10,
            'source_object_key' => $sourceObjectKey,
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }

    /** Pull the numeric value out of a named KPI tile. */
    private function kpiValue(AssertableInertia $page, string $label): string
    {
        /** @var list<array{label: string, value: string}> $kpis */
        $kpis = $page->toArray()['props']['kpis'];
        foreach ($kpis as $kpi) {
            if ($kpi['label'] === $label) {
                return $kpi['value'];
            }
        }

        $this->fail("KPI {$label} not present on the Overview page");
    }

    public function test_reports_kpi_counts_only_this_projects_reports(): void
    {
        $mine = $this->makeProject();
        $other = $this->makeProject();

        $this->insertReport($mine, 'Mine A');
        $this->insertReport($mine, 'Mine B');
        $this->insertReport($other, 'Someone else A');
        $this->insertReport($other, 'Someone else B');
        $this->insertReport($other, 'Someone else C');

        $response = $this->actingAs($this->user)->get('/projects/'.$mine->slug);

        $response->assertStatus(200);
        $response->assertInertia(function (AssertableInertia $page) {
            // 2, not 5 — the other project's three reports must not be counted.
            $this->assertSame('2', $this->kpiValue($page, 'REPORTS'));
        });
    }

    public function test_project_with_no_reports_reports_zero_despite_other_projects(): void
    {
        $empty = $this->makeProject();
        $stocked = $this->makeProject();

        $this->insertReport($stocked, 'Not mine');

        $response = $this->actingAs($this->user)->get('/projects/'.$empty->slug);

        $response->assertStatus(200);
        $response->assertInertia(function (AssertableInertia $page) {
            $this->assertSame('0', $this->kpiValue($page, 'REPORTS'));
        });
    }

    public function test_empty_project_is_offered_the_cold_start_next_action(): void
    {
        // The bug's second effect: with an unscoped count, a brand-new project
        // could never reach the $reportsCount === 0 branch once any other
        // project had ingested anything, so a genuinely empty project was
        // never told to connect a data source.
        $empty = $this->makeProject();
        $stocked = $this->makeProject();
        $this->insertReport($stocked, 'Not mine');

        $response = $this->actingAs($this->user)->get('/projects/'.$empty->slug);

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page->where(
                'next_action.title',
                'Connect your first data source',
            ),
        );
    }

    public function test_average_confidence_is_a_dash_when_nothing_has_been_answered(): void
    {
        // Was number_format(0.0, 2) — "0.00" under the label AVG CONFIDENCE,
        // on a project that has never been asked a question. That reads as
        // an appalling score, not as an absence of data, and it sits on the
        // first screen a new user sees.
        $project = $this->makeProject();

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertStatus(200)
            ->assertInertia(function (AssertableInertia $page) {
                $this->assertSame('—', $this->kpiValue($page, 'AVG CONFIDENCE'));
            });
    }

    public function test_the_project_prop_carries_what_the_edit_sheet_prefills(): void
    {
        // Components/EditProjectSheet.tsx starts from these values; without
        // `company` the Operator field would open blank and a save would
        // clear the column.
        $project = $this->makeProject();

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertStatus(200)
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('project.project_name', $project->project_name)
                ->where('project.company', $project->company)
                ->where('project.commodity', $project->commodity)
                ->where('project.region', $project->region));
    }

    public function test_the_next_action_never_points_at_the_corpus_redirect(): void
    {
        // /projects/{slug}/corpus is a 302 to /reports (merged 2026-08-18).
        // Linking it from the primary CTA on the project landing page costs
        // a round-trip and labels the destination "Reader", a page that no
        // longer exists.
        $project = $this->makeProject();
        $this->insertReport($project, 'Something to read');

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertStatus(200)
            ->assertInertia(function (AssertableInertia $page) {
                $href = (string) $page->toArray()['props']['next_action']['href'];
                $this->assertStringNotContainsString('/corpus', $href);
            });
    }

    /**
     * Insert one report and N passages against it, with a given engine.
     *
     * @param array<string, int> $byMethod ocr_method => passage count
     * @param array<string, int> $flagged ocr_method => how many of those
     *                                    carry ocr_status low_confidence
     */
    private function insertPassages(
        Project $project,
        string $title,
        array $byMethod,
        array $flagged = [],
        string $modality = 'text',
    ): string {
        $reportId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $reportId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $project->project_id,
            'title' => $title,
            'page_count' => 10,
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $ordinal = 0;
        foreach ($byMethod as $method => $count) {
            $remainingFlags = $flagged[$method] ?? 0;
            for ($i = 0; $i < $count; $i++) {
                DB::table('silver.document_passages')->insert([
                    'passage_id' => (string) Str::uuid(),
                    'document_id' => $reportId,
                    'workspace_id' => $this->workspaceId,
                    'revision_number' => 1,
                    'text' => "passage {$ordinal} of {$title}",
                    // The column carries CHECK text_hash ~ '^[0-9a-f]{64}$',
                    // so a plain uniqid() will not do.
                    'text_hash' => substr(hash('sha256', $title.$method.$ordinal), 0, 64),
                    'ordinal' => $ordinal,
                    'ocr_method' => $method === 'unknown' ? null : $method,
                    'ocr_status' => $remainingFlags-- > 0 ? 'low_confidence' : 'accepted',
                    'modality' => $modality,
                    // CHECK document_passages_image_requires_source: an
                    // image passage with no page and no object key is a
                    // row retrieval can surface and the UI cannot render,
                    // so the table refuses it. Text passages leave both
                    // null, which is what the constraint permits.
                    'page_number' => $modality === 'image' ? $ordinal + 1 : null,
                    'image_object_key' => $modality === 'image'
                        ? "page-images/{$reportId}/{$ordinal}.webp"
                        : null,
                ]);
                $ordinal++;
            }
        }

        return $reportId;
    }

    /** @return array<string, mixed> */
    private function coverage(AssertableInertia $page): array
    {
        /** @var array<string, mixed> $c */
        $c = $page->toArray()['props']['ocr_coverage'];

        return $c;
    }

    public function test_ocr_coverage_splits_engine_from_text_layer(): void
    {
        $project = $this->makeProject();
        $this->insertPassages($project, 'Scanned 43-101', [
            'fitz_native' => 4,
            'tesseract' => 3,
            'document_intelligence' => 2,
            'cohere_parse' => 2,
        ]);

        $response = $this->actingAs($this->user)->get('/projects/'.$project->slug);

        $response->assertStatus(200);
        $response->assertInertia(function (AssertableInertia $page) {
            $c = $this->coverage($page);
            $this->assertSame(11, $c['total']);
            $this->assertSame(7, $c['ocr_total'], 'tesseract + document_intelligence + cohere_parse');
            $this->assertSame(4, $c['native_total'], 'fitz_native reads a text layer');
            $this->assertSame(0, $c['unknown_total']);

            $byMethod = collect($c['by_method'])->keyBy('method');
            $this->assertSame('Cohere Parse', $byMethod['cohere_parse']['label']);
            $this->assertTrue($byMethod['cohere_parse']['is_ocr'], 'cohere_parse ran OCR, not a text layer');
        });
    }

    public function test_ocr_coverage_counts_router_flags_per_engine(): void
    {
        $project = $this->makeProject();
        $this->insertPassages(
            $project,
            'Mixed quality',
            ['tesseract' => 5, 'document_intelligence' => 4],
            ['tesseract' => 3, 'document_intelligence' => 1],
        );

        $response = $this->actingAs($this->user)->get('/projects/'.$project->slug);

        $response->assertInertia(function (AssertableInertia $page) {
            $c = $this->coverage($page);
            $this->assertSame(4, $c['flagged_total']);

            $byMethod = collect($c['by_method'])->keyBy('method');
            $this->assertSame(3, $byMethod['tesseract']['flagged']);
            $this->assertSame(1, $byMethod['document_intelligence']['flagged']);
            $this->assertTrue($byMethod['tesseract']['is_ocr']);
        });
    }

    public function test_ocr_coverage_is_scoped_to_this_project(): void
    {
        $mine = $this->makeProject();
        $other = $this->makeProject();

        $this->insertPassages($mine, 'Mine', ['tesseract' => 2]);
        $this->insertPassages($other, 'Theirs', ['tesseract' => 50]);

        $response = $this->actingAs($this->user)->get('/projects/'.$mine->slug);

        $response->assertInertia(function (AssertableInertia $page) {
            // 2, not 52. Same class of bug as the REPORTS KPI above, which
            // is why this assertion exists at all.
            $this->assertSame(2, $this->coverage($page)['total']);
        });
    }

    public function test_page_image_passages_are_not_counted_as_ocr(): void
    {
        // A page_image passage is a vision model's DESCRIPTION of a rendered
        // page. No OCR ran on it, so counting it would dilute the
        // denominator with pages that were never OCR candidates.
        $project = $this->makeProject();
        $this->insertPassages($project, 'Text', ['tesseract' => 3]);
        $this->insertPassages($project, 'Images', ['tesseract' => 7], [], 'image');

        $response = $this->actingAs($this->user)->get('/projects/'.$project->slug);

        $response->assertInertia(function (AssertableInertia $page) {
            $this->assertSame(3, $this->coverage($page)['total']);
        });
    }

    public function test_a_null_ocr_method_gets_its_own_bucket(): void
    {
        // Passages predating the 2026-05-22 ocr_method column exist. Folding
        // them into "native" would overstate how much of the corpus skipped
        // OCR, which is the one number this card is for.
        $project = $this->makeProject();
        $this->insertPassages($project, 'Legacy', [
            'unknown' => 6,
            'fitz_native' => 2,
        ]);

        $response = $this->actingAs($this->user)->get('/projects/'.$project->slug);

        $response->assertInertia(function (AssertableInertia $page) {
            $c = $this->coverage($page);
            $this->assertSame(6, $c['unknown_total']);
            $this->assertSame(2, $c['native_total']);
            $this->assertSame(0, $c['ocr_total']);

            $byMethod = collect($c['by_method'])->keyBy('method');
            $this->assertFalse(
                $byMethod['unknown']['is_ocr'],
                'not recorded is not the same as OCR ran',
            );
        });
    }

    public function test_coverage_never_reports_a_measured_accuracy(): void
    {
        // The card must not be readable as an accuracy figure. There is no
        // ground-truth page set and no CER/WER harness, so engine choice,
        // DPI and the routing thresholds are all unmeasured — a geologist
        // reading "98% coverage" as "98% correct" is exactly the misreading
        // this key exists to block.
        $project = $this->makeProject();
        $this->insertPassages($project, 'Anything', ['tesseract' => 3]);

        $response = $this->actingAs($this->user)->get('/projects/'.$project->slug);

        $response->assertInertia(function (AssertableInertia $page) {
            $this->assertNull($this->coverage($page)['measured_accuracy']);
        });
    }

    public function test_a_project_with_no_corpus_reports_zeroes_not_an_error(): void
    {
        $project = $this->makeProject();

        $response = $this->actingAs($this->user)->get('/projects/'.$project->slug);

        $response->assertStatus(200);
        $response->assertInertia(function (AssertableInertia $page) {
            $c = $this->coverage($page);
            $this->assertSame(0, $c['total']);
            $this->assertSame([], $c['by_method']);
        });
    }

    // ── Ingest summary tile ─────────────────────────────────────────────
    //
    // 2026-09-29 ("waffles"): the tile read "10 files ingesting · latest:
    // …MadsenPFS_NI43-101…" for reports that had finished. It was computed
    // separately from the Ingestion Runs page, by matching bronze filenames
    // against report TITLES, never consulted silver.ingest_progress, and was
    // only corrected by the frontend's first poll 5-30 s later.

    public function test_a_finished_report_is_not_counted_as_ingesting(): void
    {
        $project = $this->makeProject();
        $key = "reports/{$project->project_id}/20260929_012744_SRKCA_MadsenPFS_NI43-101_CAPR003299_Final_20250218.pdf";
        Storage::disk('s3-bronze')->put($key, 'fake-pdf-bytes');
        $this->insertReport($project, 'Madsen Mine Pre-Feasibility Study Technical Report', $key);

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('ingest_summary.in_flight', 0)
                ->where('ingest_summary.completed', 1)
                ->where('ingest_summary.latest_in_flight', null));
    }

    public function test_the_tile_says_what_the_ingestion_runs_poll_will_say(): void
    {
        $project = $this->makeProject();
        $pid = $project->project_id;

        // Finished: the report carries its key; its title is unrelated.
        $done = "reports/{$pid}/20260929_010000_Done.pdf";
        Storage::disk('s3-bronze')->put($done, 'x');
        $this->insertReport($project, 'An Extracted Title', $done);

        // Uploaded, no report, no progress row yet: in flight.
        Storage::disk('s3-bronze')->put("reports/{$pid}/20260929_020000_Queued.pdf", 'x');

        // Completed run for a non-report file: not in flight.
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $pid,
            'minio_key' => "collars/{$pid}/20260929_030000_collars.csv",
            'filename' => '20260929_030000_collars.csv',
            'current_step' => 'completed',
            'step_index' => 5,
            'total_steps' => 5,
            'status' => 'completed',
            'started_at' => now(),
            'updated_at' => now(),
            'completed_at' => now(),
        ]);

        // Running: in flight, and the newest moving row.
        DB::table('silver.ingest_progress')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $pid,
            'minio_key' => "reports/{$pid}/20260929_040000_Parsing.pdf",
            'filename' => '20260929_040000_Parsing.pdf',
            'current_step' => 'parse',
            'step_index' => 2,
            'total_steps' => 5,
            'status' => 'started',
            'started_at' => now()->addMinute(),
            'updated_at' => now()->addMinute(),
        ]);

        $summary = null;
        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertOk()
            ->assertInertia(function (AssertableInertia $page) use (&$summary) {
                $summary = $page->toArray()['props']['ingest_summary'];
            });

        $poll = $this->actingAs($this->user)
            ->getJson("/projects/{$project->slug}/ingestion-runs.json")
            ->assertOk();

        // Exactly what Overview.tsx derives from the poll body.
        $this->assertSame([
            'in_flight' => $poll->json('runs.totals.in_flight'),
            'completed' => $poll->json('runs.totals.completed'),
            'latest_in_flight' => $poll->json('runs.latest_in_flight'),
        ], $summary);
        $this->assertSame(2, $summary['in_flight']);
        $this->assertSame(1, $summary['completed']);
        $this->assertSame('20260929_040000_Parsing.pdf', $summary['latest_in_flight']);
    }

    public function test_recent_activity_ids_are_the_audit_ids_and_unique(): void
    {
        // QueryAuditLog's key is audit_id. The mapper read `$r->id`, which does
        // not exist, so every row got '' and React keyed all of them the same.
        $project = $this->makeProject();
        $first = QueryAuditLog::create([
            'user_id' => $this->user->id,
            'project_id' => $project->project_id,
            'query_id' => (string) Str::uuid(),
            'query_text' => 'first question',
            'response_text' => 'an answer',
            'ip_address' => '127.0.0.1',
            'llm_model' => 'command-a-plus-05-2026',
        ]);
        $second = QueryAuditLog::create([
            'user_id' => $this->user->id,
            'project_id' => $project->project_id,
            'query_id' => (string) Str::uuid(),
            'query_text' => 'second question',
            'ip_address' => '127.0.0.1',
            'llm_model' => 'command-a-plus-05-2026',
        ]);

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertOk()
            ->assertInertia(function (AssertableInertia $page) use ($first, $second) {
                $ids = array_column($page->toArray()['props']['recent_activity'], 'id');
                $this->assertCount(2, $ids);
                $this->assertNotContains('', $ids);
                $this->assertEqualsCanonicalizing([(string) $first->audit_id, (string) $second->audit_id], $ids);
            });
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function nonCollarNonReportData(): array
    {
        return [
            'spatial features' => ['spatial_features'],
            'attribute tables' => ['attribute_tables'],
        ];
    }

    #[DataProvider('nonCollarNonReportData')]
    public function test_a_project_holding_only_other_data_is_not_told_to_connect_a_first_source(string $kind): void
    {
        $project = $this->makeProject();

        if ($kind === 'spatial_features') {
            DB::table('silver.spatial_features')->insert([
                'feature_id' => (string) Str::uuid(),
                'workspace_id' => $this->workspaceId,
                'project_id' => $project->project_id,
                'feature_type' => 'fault',
                'properties' => '{}',
            ]);
        } else {
            DB::table('silver.attribute_tables')->insert([
                'workspace_id' => $this->workspaceId,
                'project_id' => $project->project_id,
                'source_file_sha256' => str_repeat('a', 64),
                'source_layer' => 'claims',
                'row_index' => 0,
                'attributes' => '{}',
            ]);
        }

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertOk()
            ->assertInertia(function (AssertableInertia $page) {
                $props = $page->toArray()['props'];
                $this->assertNotSame('Connect your first data source', $props['next_action']['title']);
                $this->assertFalse($props['empty']);
            });
    }

    public function test_a_genuinely_empty_project_is_still_empty(): void
    {
        $project = $this->makeProject();

        $this->actingAs($this->user)
            ->get('/projects/'.$project->slug)
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('empty', true)
                ->where('next_action.title', 'Connect your first data source'));
    }
}
