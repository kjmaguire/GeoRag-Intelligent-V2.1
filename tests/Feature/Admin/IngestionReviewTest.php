<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Master-plan §3 Step 8 — Silver Review Queue dashboard feature test
 * (doc-phase 58 scaffold).
 *
 * Assertions:
 *   - guest → 302 to /login
 *   - non-admin → 403
 *   - admin without queue data → 200 + empty queue array
 *   - admin with seeded review rows → row visible in queue + summary counts
 *   - filter by status narrows the queue
 *
 * Gated on PG via RequiresPostgres because the controller reads from
 * silver.low_confidence_page_reviews + silver.ocr_page_quality
 * (both added in doc-phase 50 migrations).
 */
class IngestionReviewTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const ENDPOINT = '/admin/ingestion-review';

    public function test_guest_is_redirected_to_login(): void
    {
        $response = $this->get(self::ENDPOINT);
        $response->assertRedirect('/login');
    }

    public function test_non_admin_user_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');

        $response = $this->get(self::ENDPOINT);
        $response->assertForbidden();
    }

    public function test_admin_with_empty_queue_sees_empty_page(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $response = $this->get(self::ENDPOINT);
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/IngestionReview')
            ->has('queue')
            ->has('summary')
            ->has('available_reasons')
            ->has('available_statuses')
        );
    }

    public function test_seeded_review_row_appears_in_queue(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $seed = $this->seedReviewRow();

        $response = $this->get(self::ENDPOINT);
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/IngestionReview')
            ->where('queue.0.review_item_id', $seed['review_item_id'])
            ->where('queue.0.report_id', $seed['report_id'])
            ->where('queue.0.page', $seed['page'])
            ->where('queue.0.reason', 'ocr_confidence_below_threshold')
            ->where('queue.0.status', 'pending')
            ->where('summary.total_pending', fn ($v) => $v >= 1)
        );
    }

    public function test_status_filter_narrows_queue(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $this->seedReviewRow();  // a pending row

        // Filter to "resolved_accept" — should be 0 rows
        $response = $this->get(self::ENDPOINT.'?status=resolved_accept');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->where('queue', [])
            ->where('filters.status', 'resolved_accept')
        );
    }

    public function test_invalid_status_filter_rejects(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $response = $this->get(self::ENDPOINT.'?status=invalid_status_xyz');
        $response->assertSessionHasErrors('status');
    }

    public function test_show_returns_json_for_existing_review_item(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $seed = $this->seedReviewRow();

        $response = $this->get('/admin/ingestion-review/'.$seed['review_item_id'].'.json');
        $response->assertOk();
        $response->assertJsonStructure([
            'review' => ['review_item_id', 'report_id', 'page', 'workspace_id', 'reason', 'status'],
            'report' => ['report_id', 'title'],
            'page_quality' => ['ocr_confidence', 'parser_used', 'retry_count'],
            'extractions',
            'ocr_results',
            'layouts',
            'parser_runs',
            'page_render_url',
        ]);
        $response->assertJson([
            'review' => [
                'review_item_id' => $seed['review_item_id'],
                'page' => 0,
                'reason' => 'ocr_confidence_below_threshold',
                'status' => 'pending',
            ],
        ]);
    }

    public function test_show_returns_404_for_unknown_review_item(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $bogusId = '00000000-0000-0000-0000-deadbeefdead';
        $response = $this->get('/admin/ingestion-review/'.$bogusId.'.json');
        $response->assertNotFound();
    }

    public function test_show_returns_404_for_malformed_uuid(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $response = $this->get('/admin/ingestion-review/not-a-uuid.json');
        // Route pattern excludes non-UUIDs → 404 from route resolution
        $response->assertNotFound();
    }

    public function test_show_requires_admin(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');

        $bogusId = '00000000-0000-0000-0000-000000000000';
        $response = $this->get('/admin/ingestion-review/'.$bogusId.'.json');
        $response->assertForbidden();
    }

    public function test_page_render_404s_when_review_item_missing(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $bogusId = '00000000-0000-0000-0000-deadbeefdead';
        $response = $this->get('/admin/ingestion-review/'.$bogusId.'/page/0.png');
        $response->assertNotFound();
    }

    public function test_update_applies_resolved_accept_disposition(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $seed = $this->seedReviewRow();

        $response = $this->patchJson('/admin/ingestion-review/'.$seed['review_item_id'], [
            'status' => 'resolved_accept',
            'resolution_notes' => 'reviewed; OCR was correct on this page',
        ]);
        $response->assertOk();
        $response->assertJson([
            'review_item_id' => $seed['review_item_id'],
            'status' => 'resolved_accept',
            'resolution_notes' => 'reviewed; OCR was correct on this page',
        ]);

        // Verify the row updated server-side.
        $row = DB::connection('pgsql')
            ->table('silver.low_confidence_page_reviews')
            ->where('review_item_id', $seed['review_item_id'])
            ->first(['status', 'resolution_notes', 'resolved_at', 'assigned_to']);
        $this->assertNotNull($row);
        $this->assertSame('resolved_accept', $row->status);
        $this->assertSame('reviewed; OCR was correct on this page', $row->resolution_notes);
        $this->assertNotNull($row->resolved_at);
        $this->assertSame((int) $admin->id, (int) $row->assigned_to);
    }

    public function test_update_rejects_invalid_status_value(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $seed = $this->seedReviewRow();

        $response = $this->patchJson('/admin/ingestion-review/'.$seed['review_item_id'], [
            'status' => 'definitely_not_a_valid_status',
        ]);
        $response->assertStatus(422);
        $response->assertJsonValidationErrors('status');
    }

    public function test_update_blocks_transition_out_of_resolved(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $seed = $this->seedReviewRow();

        // First resolve.
        $this->patchJson('/admin/ingestion-review/'.$seed['review_item_id'], [
            'status' => 'resolved_accept',
        ])->assertOk();

        // Now try to flip back to pending.
        $response = $this->patchJson('/admin/ingestion-review/'.$seed['review_item_id'], [
            'status' => 'pending',
        ]);
        $response->assertStatus(422);
        $response->assertJsonStructure(['error', 'current_status']);
    }

    public function test_update_404s_for_unknown_review_item(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');

        $bogusId = '00000000-0000-0000-0000-deadbeefdead';
        $response = $this->patchJson('/admin/ingestion-review/'.$bogusId, [
            'status' => 'resolved_accept',
        ]);
        $response->assertNotFound();
    }

    public function test_update_requires_admin(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');

        $bogusId = '00000000-0000-0000-0000-000000000000';
        $response = $this->patchJson('/admin/ingestion-review/'.$bogusId, [
            'status' => 'resolved_accept',
        ]);
        $response->assertForbidden();
    }

    /**
     * @return array{review_item_id: string, report_id: string, workspace_id: string, page: int}
     */
    private function seedReviewRow(): array
    {
        // Use the existing dev workspace (a0000000-...) — assumes it exists in the test DB.
        // (RequiresPostgres uses the project's actual PG; the seeded workspaces are present.)
        $workspaceId = '00000000-0000-0000-0000-00000000aaaa';

        // Insert a workspace if missing — idempotent.
        DB::connection('pgsql')->statement(
            "INSERT INTO silver.workspaces (workspace_id, name)
             VALUES (?::uuid, 'phase58-review-test')
             ON CONFLICT DO NOTHING",
            [$workspaceId]
        );

        // Insert a silver.reports row
        $reportId = '11111111-1111-1111-1111-' . substr(md5(uniqid('', true)), 0, 12);
        DB::connection('pgsql')->statement(
            "INSERT INTO silver.reports (report_id, title) VALUES (?::uuid, ?)",
            [$reportId, 'phase58-review-test-report']
        );

        // Insert a review row
        $reviewItemId = (string) \Illuminate\Support\Str::uuid();
        DB::connection('pgsql')->statement(
            "INSERT INTO silver.low_confidence_page_reviews
                (review_item_id, report_id, page, workspace_id, reason, status)
             VALUES (?::uuid, ?::uuid, ?, ?::uuid, ?, ?)",
            [$reviewItemId, $reportId, 0, $workspaceId, 'ocr_confidence_below_threshold', 'pending']
        );

        // Also seed a per-page quality row to populate the JOIN
        DB::connection('pgsql')->statement(
            "INSERT INTO silver.ocr_page_quality
                (report_id, page, workspace_id, ocr_confidence, parser_used, retry_count, needs_review)
             VALUES (?::uuid, ?, ?::uuid, ?, ?, ?, ?)",
            [$reportId, 0, $workspaceId, 0.62, 'scanned_paddleocr', 2, true]
        );

        return [
            'review_item_id' => $reviewItemId,
            'report_id' => $reportId,
            'workspace_id' => $workspaceId,
            'page' => 0,
        ];
    }
}
