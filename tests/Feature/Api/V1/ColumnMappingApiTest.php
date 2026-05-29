<?php

namespace Tests\Feature\Api\V1;

use App\Models\ColumnMapping;
use App\Models\User;
use App\Models\VendorProfile;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Feature tests for ColumnMappingController (nested under vendor-profiles).
 *
 * Coverage targets (16 tests):
 *   - Unauthenticated request returns 401
 *   - Index returns mappings for the profile
 *   - Index filtered by parser_type
 *   - Non-admin store returns 403
 *   - Admin store creates mapping and returns 201
 *   - Store rejects invalid parser_type with 422 (admin)
 *   - Store duplicate (profile, parser_type, canonical_field) returns 422 (admin)
 *   - Store duplicate (profile, parser_type, source_column) returns 422 (admin)
 *   - Non-admin update returns 403
 *   - Admin update modifies and returns 200
 *   - Update with wrong profile prefix returns 404 (admin)
 *   - Non-admin destroy returns 403
 *   - Admin destroy deletes mapping
 *   - Destroy with wrong profile prefix returns 404 (admin)
 */
class ColumnMappingApiTest extends TestCase
{
    use RefreshDatabase;

    private User $user;
    private User $admin;
    private VendorProfile $profile;

    protected function setUp(): void
    {
        parent::setUp();
        $this->user    = User::factory()->create();
        $this->admin   = User::factory()->admin()->create();
        $this->profile = VendorProfile::factory()->create();
    }

    private function baseUrl(): string
    {
        return "/api/v1/vendor-profiles/{$this->profile->id}/column-mappings";
    }

    // -------------------------------------------------------------------------
    // Auth guard
    // -------------------------------------------------------------------------

    public function test_unauthenticated_request_returns_401(): void
    {
        $this->getJson($this->baseUrl())
            ->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // index
    // -------------------------------------------------------------------------

    public function test_index_returns_mappings_for_profile(): void
    {
        ColumnMapping::factory()->count(3)->create(['vendor_profile_id' => $this->profile->id]);

        // Mapping belonging to a different profile — must NOT appear.
        ColumnMapping::factory()->create();

        $response = $this->actingAs($this->user)
            ->getJson($this->baseUrl());

        $response->assertOk();
        $this->assertCount(3, $response->json());
    }

    public function test_index_filtered_by_parser_type(): void
    {
        ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
            'parser_type'       => 'csv_sample',
            'canonical_field'   => 'au_ppm',
            'source_column'     => 'Au_ppm',
        ]);
        ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
            'parser_type'       => 'csv_collar',
            'canonical_field'   => 'hole_id',
            'source_column'     => 'HoleID',
        ]);

        $response = $this->actingAs($this->user)
            ->getJson($this->baseUrl() . '?parser_type=csv_sample');

        $response->assertOk();
        $this->assertCount(1, $response->json());
        $this->assertSame('csv_sample', $response->json('0.parser_type'));
    }

    // -------------------------------------------------------------------------
    // store
    // -------------------------------------------------------------------------

    public function test_non_admin_store_returns_403(): void
    {
        $response = $this->actingAs($this->user)
            ->postJson($this->baseUrl(), [
                'parser_type'     => 'csv_sample',
                'canonical_field' => 'au_ppm',
                'source_column'   => 'Gold_ppm',
            ]);

        $response->assertForbidden();
        $this->assertDatabaseMissing('column_mappings', ['canonical_field' => 'au_ppm']);
    }

    public function test_admin_store_creates_mapping_and_returns_201(): void
    {
        $payload = [
            'parser_type'     => 'csv_sample',
            'canonical_field' => 'au_ppm',
            'source_column'   => 'Gold_ppm',
            'source_unit'     => 'ppm',
            'target_unit'     => 'g/t',
            'notes'           => 'ALS standard Au assay column',
        ];

        $response = $this->actingAs($this->admin)
            ->postJson($this->baseUrl(), $payload);

        $response->assertCreated()
            ->assertJsonPath('parser_type', 'csv_sample')
            ->assertJsonPath('canonical_field', 'au_ppm')
            ->assertJsonPath('vendor_profile_id', $this->profile->id);

        $this->assertDatabaseHas('column_mappings', [
            'vendor_profile_id' => $this->profile->id,
            'canonical_field'   => 'au_ppm',
        ]);
    }

    public function test_store_rejects_invalid_parser_type_with_422(): void
    {
        $response = $this->actingAs($this->admin)
            ->postJson($this->baseUrl(), [
                'parser_type'     => 'not_a_real_type',
                'canonical_field' => 'au_ppm',
                'source_column'   => 'Au',
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['parser_type']);
    }

    public function test_store_duplicate_canonical_field_returns_422(): void
    {
        // Seed a mapping with the same (profile, parser_type, canonical_field).
        ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
            'parser_type'       => 'csv_sample',
            'canonical_field'   => 'au_ppm',
            'source_column'     => 'Au_existing',
        ]);

        // Application-layer validation detects the duplicate and returns 422.
        // On real Postgres, the DB's unique index would additionally produce a
        // 409 if the validation layer were bypassed — the controller catches
        // UniqueConstraintViolationException as a belt-and-suspenders guard.
        $response = $this->actingAs($this->admin)
            ->postJson($this->baseUrl(), [
                'parser_type'     => 'csv_sample',
                'canonical_field' => 'au_ppm',   // duplicate canonical_field
                'source_column'   => 'Gold_new', // different source column
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['canonical_field']);
    }

    public function test_store_duplicate_source_column_returns_422(): void
    {
        // Seed a mapping with the same (profile, parser_type, source_column).
        ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
            'parser_type'       => 'csv_sample',
            'canonical_field'   => 'au_ppm',
            'source_column'     => 'Gold_ppm',
        ]);

        $response = $this->actingAs($this->admin)
            ->postJson($this->baseUrl(), [
                'parser_type'     => 'csv_sample',
                'canonical_field' => 'cu_pct',   // different canonical field
                'source_column'   => 'Gold_ppm', // duplicate source_column
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['source_column']);
    }

    // -------------------------------------------------------------------------
    // update
    // -------------------------------------------------------------------------

    public function test_non_admin_update_returns_403(): void
    {
        $mapping = ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
            'parser_type'       => 'csv_sample',
            'canonical_field'   => 'au_ppm',
            'source_column'     => 'Au_orig',
        ]);

        $this->actingAs($this->user)
            ->patchJson("{$this->baseUrl()}/{$mapping->id}", ['source_column' => 'Au_blocked'])
            ->assertForbidden();

        $this->assertDatabaseHas('column_mappings', ['id' => $mapping->id, 'source_column' => 'Au_orig']);
    }

    public function test_admin_update_modifies_mapping_and_returns_200(): void
    {
        $mapping = ColumnMapping::factory()->create([
            'vendor_profile_id' => $this->profile->id,
            'parser_type'       => 'csv_sample',
            'canonical_field'   => 'au_ppm',
            'source_column'     => 'Au_orig',
        ]);

        $response = $this->actingAs($this->admin)
            ->patchJson("{$this->baseUrl()}/{$mapping->id}", [
                'source_column' => 'Au_updated',
            ]);

        $response->assertOk()
            ->assertJsonPath('source_column', 'Au_updated');

        $this->assertDatabaseHas('column_mappings', ['id' => $mapping->id, 'source_column' => 'Au_updated']);
    }

    public function test_update_with_wrong_profile_prefix_returns_404(): void
    {
        $otherProfile = VendorProfile::factory()->create();
        $mapping      = ColumnMapping::factory()->create(['vendor_profile_id' => $otherProfile->id]);

        // Attempt to update via THIS profile's URL — mapping belongs elsewhere.
        $response = $this->actingAs($this->admin)
            ->patchJson("{$this->baseUrl()}/{$mapping->id}", [
                'source_column' => 'Should Not Work',
            ]);

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // destroy
    // -------------------------------------------------------------------------

    public function test_non_admin_destroy_returns_403(): void
    {
        $mapping = ColumnMapping::factory()->create(['vendor_profile_id' => $this->profile->id]);

        $this->actingAs($this->user)
            ->deleteJson("{$this->baseUrl()}/{$mapping->id}")
            ->assertForbidden();

        $this->assertDatabaseHas('column_mappings', ['id' => $mapping->id]);
    }

    public function test_admin_destroy_deletes_mapping(): void
    {
        $mapping = ColumnMapping::factory()->create(['vendor_profile_id' => $this->profile->id]);

        $this->actingAs($this->admin)
            ->deleteJson("{$this->baseUrl()}/{$mapping->id}")
            ->assertNoContent();

        $this->assertDatabaseMissing('column_mappings', ['id' => $mapping->id]);
    }

    public function test_destroy_with_wrong_profile_prefix_returns_404(): void
    {
        $otherProfile = VendorProfile::factory()->create();
        $mapping      = ColumnMapping::factory()->create(['vendor_profile_id' => $otherProfile->id]);

        $response = $this->actingAs($this->admin)
            ->deleteJson("{$this->baseUrl()}/{$mapping->id}");

        $response->assertNotFound();
        // The mapping must still exist — we must not delete across boundaries.
        $this->assertDatabaseHas('column_mappings', ['id' => $mapping->id]);
    }
}
