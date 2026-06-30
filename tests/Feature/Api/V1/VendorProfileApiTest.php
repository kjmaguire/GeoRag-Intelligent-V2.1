<?php

namespace Tests\Feature\Api\V1;

use App\Models\ColumnMapping;
use App\Models\User;
use App\Models\VendorProfile;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Feature tests for VendorProfileController.
 *
 * Tests run against SQLite in-memory (see phpunit.xml). The vendor_profiles
 * and column_mappings tables live in the public schema (no prefix), so no
 * table-name overrides are required.
 *
 * Coverage targets (18 tests):
 *   - Unauthenticated request returns 401
 *   - Index returns paginated profiles
 *   - Index includes column_mappings_count
 *   - Index filtered by profile_type
 *   - Index filtered by is_global
 *   - Non-admin store returns 403
 *   - Admin store creates profile and returns 201
 *   - Store rejects duplicate name with 422 (admin)
 *   - Store rejects invalid profile_type with 422 (admin)
 *   - Store requires is_global (admin)
 *   - Show returns profile with mappings array
 *   - Non-admin update returns 403
 *   - Admin update modifies and returns 200
 *   - Non-admin destroy returns 403
 *   - Admin destroy deletes profile and cascades to mappings
 */
class VendorProfileApiTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private User $admin;

    protected function setUp(): void
    {
        parent::setUp();
        $this->user = User::factory()->create();
        $this->admin = User::factory()->admin()->create();
    }

    // -------------------------------------------------------------------------
    // Auth guard
    // -------------------------------------------------------------------------

    public function test_unauthenticated_request_returns_401(): void
    {
        $this->getJson('/api/v1/vendor-profiles')
            ->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // index
    // -------------------------------------------------------------------------

    public function test_index_returns_paginated_profiles(): void
    {
        VendorProfile::factory()->count(3)->create();

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/vendor-profiles');

        $response->assertOk()
            ->assertJsonStructure([
                'data' => [
                    '*' => [
                        'id',
                        'name',
                        'description',
                        'profile_type',
                        'is_global',
                        'created_by_user_id',
                        'column_mappings_count',
                        'created_at',
                        'updated_at',
                    ],
                ],
                'current_page',
                'total',
            ]);
    }

    public function test_index_includes_column_mappings_count(): void
    {
        $profile = VendorProfile::factory()->create();
        ColumnMapping::factory()->count(3)->create(['vendor_profile_id' => $profile->id]);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/vendor-profiles');

        $response->assertOk();

        $data = $response->json('data');
        $this->assertNotEmpty($data);

        $found = collect($data)->firstWhere('id', $profile->id);
        $this->assertNotNull($found, 'Profile not found in index response');
        $this->assertSame(3, $found['column_mappings_count']);
    }

    public function test_index_filtered_by_profile_type_returns_only_matching(): void
    {
        VendorProfile::factory()->lab()->create(['name' => 'Lab Profile A']);
        VendorProfile::factory()->driller()->create(['name' => 'Driller Profile B']);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/vendor-profiles?profile_type=lab');

        $response->assertOk();

        $data = $response->json('data');
        $this->assertCount(1, $data);
        $this->assertSame('lab', $data[0]['profile_type']);
    }

    public function test_index_filtered_by_is_global(): void
    {
        VendorProfile::factory()->global()->create(['name' => 'Global A']);
        VendorProfile::factory()->notGlobal()->create(['name' => 'Private B']);

        $response = $this->actingAs($this->user)
            ->getJson('/api/v1/vendor-profiles?is_global=true');

        $response->assertOk();

        $data = $response->json('data');
        $this->assertCount(1, $data);
        $this->assertTrue($data[0]['is_global']);
    }

    // -------------------------------------------------------------------------
    // store
    // -------------------------------------------------------------------------

    public function test_non_admin_store_returns_403(): void
    {
        $response = $this->actingAs($this->user)
            ->postJson('/api/v1/vendor-profiles', [
                'name' => 'Should Be Blocked',
                'profile_type' => 'lab',
                'is_global' => true,
            ]);

        $response->assertForbidden();
        $this->assertDatabaseMissing('vendor_profiles', ['name' => 'Should Be Blocked']);
    }

    public function test_admin_store_creates_profile_and_returns_201(): void
    {
        $payload = [
            'name' => 'ALS Geochemistry',
            'description' => 'Standard ALS lab output format',
            'profile_type' => 'lab',
            'is_global' => true,
        ];

        $response = $this->actingAs($this->admin)
            ->postJson('/api/v1/vendor-profiles', $payload);

        $response->assertCreated()
            ->assertJsonPath('name', 'ALS Geochemistry')
            ->assertJsonPath('profile_type', 'lab')
            ->assertJsonPath('is_global', true)
            ->assertJsonPath('created_by_user_id', $this->admin->id);

        $this->assertDatabaseHas('vendor_profiles', ['name' => 'ALS Geochemistry']);
    }

    public function test_store_rejects_duplicate_name_with_422(): void
    {
        VendorProfile::factory()->create(['name' => 'Duplicate Name']);

        $response = $this->actingAs($this->admin)
            ->postJson('/api/v1/vendor-profiles', [
                'name' => 'Duplicate Name',
                'profile_type' => 'lab',
                'is_global' => false,
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['name']);
    }

    public function test_store_rejects_invalid_profile_type_with_422(): void
    {
        $response = $this->actingAs($this->admin)
            ->postJson('/api/v1/vendor-profiles', [
                'name' => 'Bad Type Profile',
                'profile_type' => 'totally_made_up',
                'is_global' => false,
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['profile_type']);
    }

    public function test_store_requires_is_global(): void
    {
        $response = $this->actingAs($this->admin)
            ->postJson('/api/v1/vendor-profiles', [
                'name' => 'Missing Global Flag',
                'profile_type' => 'internal',
                // is_global intentionally omitted
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['is_global']);
    }

    // -------------------------------------------------------------------------
    // show
    // -------------------------------------------------------------------------

    public function test_show_returns_profile_with_nested_mappings(): void
    {
        $profile = VendorProfile::factory()->create();
        ColumnMapping::factory()->count(2)->create(['vendor_profile_id' => $profile->id]);

        $response = $this->actingAs($this->user)
            ->getJson("/api/v1/vendor-profiles/{$profile->id}");

        $response->assertOk()
            ->assertJsonPath('id', $profile->id)
            ->assertJsonStructure(['mappings' => [['id', 'parser_type', 'canonical_field', 'source_column']]]);

        $this->assertCount(2, $response->json('mappings'));
    }

    // -------------------------------------------------------------------------
    // update
    // -------------------------------------------------------------------------

    public function test_non_admin_update_returns_403(): void
    {
        $profile = VendorProfile::factory()->create(['name' => 'Original Name']);

        $this->actingAs($this->user)
            ->patchJson("/api/v1/vendor-profiles/{$profile->id}", ['name' => 'Blocked Rename'])
            ->assertForbidden();

        $this->assertDatabaseHas('vendor_profiles', ['name' => 'Original Name']);
    }

    public function test_admin_update_modifies_profile_and_returns_200(): void
    {
        $profile = VendorProfile::factory()->create(['name' => 'Original Name']);

        $response = $this->actingAs($this->admin)
            ->patchJson("/api/v1/vendor-profiles/{$profile->id}", [
                'name' => 'Renamed Profile',
            ]);

        $response->assertOk()
            ->assertJsonPath('name', 'Renamed Profile');

        $this->assertDatabaseHas('vendor_profiles', ['name' => 'Renamed Profile']);
    }

    // -------------------------------------------------------------------------
    // destroy + cascade
    // -------------------------------------------------------------------------

    public function test_non_admin_destroy_returns_403(): void
    {
        $profile = VendorProfile::factory()->create();

        $this->actingAs($this->user)
            ->deleteJson("/api/v1/vendor-profiles/{$profile->id}")
            ->assertForbidden();

        $this->assertDatabaseHas('vendor_profiles', ['id' => $profile->id]);
    }

    public function test_admin_destroy_deletes_profile_and_cascades_to_mappings(): void
    {
        $profile = VendorProfile::factory()->create();
        $mapping = ColumnMapping::factory()->create(['vendor_profile_id' => $profile->id]);

        $this->actingAs($this->admin)
            ->deleteJson("/api/v1/vendor-profiles/{$profile->id}")
            ->assertNoContent();

        $this->assertDatabaseMissing('vendor_profiles', ['id' => $profile->id]);

        // CASCADE: the mapping should also be gone.
        // SQLite enforces FK cascades when foreign_key_pragmas is on (Laravel
        // enables this by default in testing since Laravel 10).
        $this->assertDatabaseMissing('column_mappings', ['id' => $mapping->id]);
    }
}
