<?php

namespace Tests\Feature\Api\V1;

use App\Models\User;
use App\Models\VendorProfile;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for VendorProfileController.
 *
 * Routes under test:
 *   GET    /api/v1/vendor-profiles            (index  — any authenticated user)
 *   GET    /api/v1/vendor-profiles/{id}       (show   — any authenticated user)
 *   POST   /api/v1/vendor-profiles            (store  — admin only)
 *   PUT    /api/v1/vendor-profiles/{id}       (update — admin only)
 *   DELETE /api/v1/vendor-profiles/{id}       (destroy — admin only)
 *
 * Scoping model: vendor profiles are GLOBAL (not project-scoped or user-owned).
 * Per the controller docblock:
 *   "Any authenticated user may read them (index/show)."
 *   "Creating, updating, or deleting a profile requires the 'admin' gate."
 *
 * The admin gate is the only IDOR surface. There is no user-to-resource ownership
 * to test — there are no "user A cannot read user B's profile" scenarios because
 * all profiles are intentionally shared across all authenticated users.
 *
 * Tests verify:
 *   1. Non-admin store/update/destroy → 403.
 *   2. index/show readable by non-admin → 200.
 *   3. Unauthenticated requests → 401.
 *
 * SQLite compatibility: no PostGIS columns. All tests run under SQLite.
 */
class VendorProfileControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $adminUser;
    private User $regularUser;
    private VendorProfile $profile;

    protected function setUp(): void
    {
        parent::setUp();

        $this->adminUser   = User::factory()->create(['is_admin' => true]);
        $this->regularUser = User::factory()->create(['is_admin' => false]);

        $this->profile = VendorProfile::factory()->create();
    }

    // -------------------------------------------------------------------------
    // Non-admin store → 403
    // -------------------------------------------------------------------------

    public function test_non_admin_cannot_create_vendor_profile(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->postJson('/api/v1/vendor-profiles', [
            'name'         => 'Stolen Profile',
            'profile_type' => 'lab',
            'is_global'    => true,
        ]);

        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // Non-admin update → 403
    // -------------------------------------------------------------------------

    public function test_non_admin_cannot_update_vendor_profile(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->patchJson("/api/v1/vendor-profiles/{$this->profile->id}", [
            'name' => 'Hijacked Name',
        ]);

        $response->assertForbidden();

        // Name must be unchanged.
        $this->assertDatabaseMissing('vendor_profiles', ['name' => 'Hijacked Name']);
    }

    // -------------------------------------------------------------------------
    // Non-admin destroy → 403
    // -------------------------------------------------------------------------

    public function test_non_admin_cannot_delete_vendor_profile(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->deleteJson("/api/v1/vendor-profiles/{$this->profile->id}");

        $response->assertForbidden();

        // Profile must still exist.
        $this->assertDatabaseHas('vendor_profiles', ['id' => $this->profile->id]);
    }

    // -------------------------------------------------------------------------
    // Sanity: index is readable by any authenticated user (non-admin)
    // -------------------------------------------------------------------------

    public function test_non_admin_can_list_vendor_profiles(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->getJson('/api/v1/vendor-profiles');

        $response->assertOk();
    }

    // -------------------------------------------------------------------------
    // Sanity: show is readable by any authenticated user (non-admin)
    // -------------------------------------------------------------------------

    public function test_non_admin_can_show_vendor_profile(): void
    {
        $this->actingAs($this->regularUser, 'sanctum');

        $response = $this->getJson("/api/v1/vendor-profiles/{$this->profile->id}");

        $response->assertOk()
            ->assertJsonPath('id', $this->profile->id);
    }

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated requests → 401
    // -------------------------------------------------------------------------

    public function test_unauthenticated_index_returns_401(): void
    {
        $response = $this->getJson('/api/v1/vendor-profiles');

        $response->assertUnauthorized();
    }
}
