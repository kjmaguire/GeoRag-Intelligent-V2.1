<?php

namespace Tests\Feature\Api\V1;

use App\Models\ColumnMapping;
use App\Models\User;
use App\Models\VendorProfile;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for ColumnMappingController.
 *
 * Routes under test (all nested under vendor-profiles):
 *   GET    /api/v1/vendor-profiles/{vendor_profile}/column-mappings         (index)
 *   POST   /api/v1/vendor-profiles/{vendor_profile}/column-mappings         (store)
 *   PATCH  /api/v1/vendor-profiles/{vendor_profile}/column-mappings/{id}    (update)
 *   DELETE /api/v1/vendor-profiles/{vendor_profile}/column-mappings/{id}    (destroy)
 *
 * Scoping model: vendor profiles are GLOBAL (not project-scoped or user-owned).
 * Per the controller docblock:
 *   "Index is readable by any authenticated user."
 *   "store/update/destroy require the 'admin' gate (is_admin = true)."
 *
 * The admin gate is the IDOR surface here: a non-admin user must NOT be able
 * to mutate any vendor profile's column mappings, regardless of which profile
 * UUID they use. The test verifies that a non-admin user receives 403 on
 * store/update/destroy. A passing test for a broken gate would mask a real IDOR.
 *
 * The cross-profile URL-manipulation guard (update/destroy check
 * $columnMapping->vendor_profile_id !== $vendorProfile->id) is also tested
 * to ensure it returns 404 and does not mutate the wrong profile's mappings.
 *
 * SQLite compatibility: vendor_profiles and column_mappings have no PostGIS
 * columns. All tests run under SQLite.
 */
class ColumnMappingControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $adminUser;
    private User $regularUser;
    private VendorProfile $profile;

    protected function setUp(): void
    {
        parent::setUp();

        // Admin user (is_admin = true).
        $this->adminUser   = User::factory()->create(['is_admin' => true]);
        // Regular non-admin user.
        $this->regularUser = User::factory()->create(['is_admin' => false]);

        $this->profile = VendorProfile::factory()->create();
    }

    // -------------------------------------------------------------------------
    // Non-admin store → 403 (admin gate enforced on mutations)
    // -------------------------------------------------------------------------

    public function test_non_admin_cannot_create_column_mapping(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->postJson(
            "/api/v1/vendor-profiles/{$this->profile->id}/column-mappings",
            [
                'parser_type'     => 'csv_collar',
                'source_column'   => 'DrillHoleID',
                'canonical_field' => 'hole_id',
            ]
        );

        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // Non-admin update → 403
    // -------------------------------------------------------------------------

    public function test_non_admin_cannot_update_column_mapping(): void
    {
        $this->actingAs($this->adminUser, 'sanctum');

        // Seed a mapping as admin.
        $mapping = ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
        ]);

        // Switch to non-admin and try to update.
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->patchJson(
            "/api/v1/vendor-profiles/{$this->profile->id}/column-mappings/{$mapping->id}",
            ['confidence_weight' => 0.5]
        );

        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // Non-admin destroy → 403
    // -------------------------------------------------------------------------

    public function test_non_admin_cannot_delete_column_mapping(): void
    {
        $this->actingAs($this->adminUser, 'sanctum');

        $mapping = ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
        ]);

        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->deleteJson(
            "/api/v1/vendor-profiles/{$this->profile->id}/column-mappings/{$mapping->id}"
        );

        $response->assertForbidden();

        // Mapping must still exist.
        $this->assertDatabaseHas('column_mappings', ['id' => $mapping->id]);
    }

    // -------------------------------------------------------------------------
    // Cross-profile URL manipulation: admin cannot move a mapping to a different
    // profile by mixing vendor_profile and column_mapping IDs from different rows.
    // -------------------------------------------------------------------------

    public function test_cross_profile_mapping_manipulation_returns_404(): void
    {
        $this->actingAs($this->adminUser, 'sanctum');

        $otherProfile = VendorProfile::factory()->create();
        $mappingOnOtherProfile = ColumnMapping::factory()->create([
            'vendor_profile_id' => $otherProfile->id,
        ]);

        // Pass profile A in the URL but mapping belonging to profile B.
        $response = $this->patchJson(
            "/api/v1/vendor-profiles/{$this->profile->id}/column-mappings/{$mappingOnOtherProfile->id}",
            ['confidence_weight' => 0.9]
        );

        $response->assertNotFound()
            ->assertJsonPath('message', 'Column mapping not found.');
    }

    // -------------------------------------------------------------------------
    // Sanity: index is readable by any authenticated user
    // -------------------------------------------------------------------------

    public function test_any_authenticated_user_can_list_column_mappings(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->getJson(
            "/api/v1/vendor-profiles/{$this->profile->id}/column-mappings"
        );

        $response->assertOk();
    }
}
