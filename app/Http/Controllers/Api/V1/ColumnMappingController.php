<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Http\Requests\StoreColumnMappingRequest;
use App\Http\Requests\UpdateColumnMappingRequest;
use App\Models\ColumnMapping;
use App\Models\VendorProfile;
use Illuminate\Database\UniqueConstraintViolationException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * RESTful API for column mappings, nested under vendor profiles.
 *
 * Mappings per profile are typically small (< 50 rows) so pagination
 * is omitted for this resource — the full list is always returned.
 *
 * Index is readable by any authenticated user. store/update/destroy
 * require the 'admin' gate (is_admin = true on the users table).
 */
class ColumnMappingController extends Controller
{
    /**
     * List all column mappings for a vendor profile.
     *
     * GET /api/v1/vendor-profiles/{vendor_profile}/column-mappings
     *
     * Optional query param:
     *   ?parser_type=csv_sample  — filter by parser type
     */
    public function index(VendorProfile $vendorProfile, Request $request): JsonResponse
    {
        $query = $vendorProfile->columnMappings()->orderBy('canonical_field');

        if ($request->filled('parser_type')) {
            $query->where('parser_type', $request->string('parser_type'));
        }

        return response()->json($query->get());
    }

    /**
     * Create a new column mapping under the given vendor profile.
     *
     * POST /api/v1/vendor-profiles/{vendor_profile}/column-mappings
     */
    public function store(StoreColumnMappingRequest $request, VendorProfile $vendorProfile): JsonResponse
    {
        $this->authorize('admin');
        try {
            $mapping = $vendorProfile->columnMappings()->create($request->validated());

            return response()->json($mapping, 201);
        } catch (UniqueConstraintViolationException) {
            return response()->json([
                'message' => 'A mapping with this (profile, parser_type, canonical_field) or '
                    . '(profile, parser_type, source_column) combination already exists.',
            ], 409);
        }
    }

    /**
     * Update an existing column mapping.
     *
     * PATCH /api/v1/vendor-profiles/{vendor_profile}/column-mappings/{column_mapping}
     *
     * Note: parser_type cannot be changed after creation. Delete and recreate
     * the mapping if rekeying is genuinely required.
     */
    public function update(
        UpdateColumnMappingRequest $request,
        VendorProfile $vendorProfile,
        ColumnMapping $columnMapping,
    ): JsonResponse {
        $this->authorize('admin');

        // Guard against URL manipulation where a mapping from a different
        // profile is passed — treat as 404 to avoid leaking existence.
        if ($columnMapping->vendor_profile_id !== $vendorProfile->id) {
            return response()->json(['message' => 'Column mapping not found.'], 404);
        }

        try {
            $columnMapping->update($request->validated());

            return response()->json($columnMapping);
        } catch (UniqueConstraintViolationException) {
            return response()->json([
                'message' => 'A mapping with this (profile, parser_type, canonical_field) or '
                    . '(profile, parser_type, source_column) combination already exists.',
            ], 409);
        }
    }

    /**
     * Delete a column mapping.
     *
     * DELETE /api/v1/vendor-profiles/{vendor_profile}/column-mappings/{column_mapping}
     */
    public function destroy(VendorProfile $vendorProfile, ColumnMapping $columnMapping): JsonResponse
    {
        $this->authorize('admin');

        // Guard against URL manipulation — same 404 pattern as update().
        if ($columnMapping->vendor_profile_id !== $vendorProfile->id) {
            return response()->json(['message' => 'Column mapping not found.'], 404);
        }

        $columnMapping->delete();

        return response()->json(null, 204);
    }
}
