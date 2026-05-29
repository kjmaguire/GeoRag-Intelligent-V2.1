<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Http\Requests\StoreVendorProfileRequest;
use App\Http\Requests\UpdateVendorProfileRequest;
use App\Models\VendorProfile;
use Illuminate\Database\UniqueConstraintViolationException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * RESTful API for vendor profiles.
 *
 * Vendor profiles are global (not project-scoped). Any authenticated user may
 * read them (index/show). Creating, updating, or deleting a profile requires
 * the 'admin' gate (is_admin = true on the users table).
 */
class VendorProfileController extends Controller
{
    /**
     * List all vendor profiles, paginated (20/page).
     *
     * GET /api/v1/vendor-profiles
     *
     * Optional query params:
     *   ?profile_type=lab    — filter by a specific type
     *   ?is_global=true      — filter to global or non-global profiles
     */
    public function index(Request $request): JsonResponse
    {
        $query = VendorProfile::withCount('columnMappings')
            ->orderBy('name');

        if ($request->filled('profile_type')) {
            $query->where('profile_type', $request->string('profile_type'));
        }

        if ($request->has('is_global')) {
            // Accept "true"/"1"/true and "false"/"0"/false from query string.
            $query->where('is_global', filter_var($request->input('is_global'), FILTER_VALIDATE_BOOLEAN));
        }

        $profiles = $query->paginate(20);

        return response()->json($profiles);
    }

    /**
     * Show a single vendor profile with its full column mapping list.
     *
     * GET /api/v1/vendor-profiles/{vendor_profile}
     */
    public function show(VendorProfile $vendorProfile): JsonResponse
    {
        $vendorProfile->load('columnMappings');

        // Expose nested mappings under 'mappings' for a cleaner response shape.
        $data = $vendorProfile->toArray();
        $data['mappings'] = $data['column_mappings'] ?? [];
        unset($data['column_mappings']);

        return response()->json($data);
    }

    /**
     * Create a new vendor profile.
     *
     * POST /api/v1/vendor-profiles
     */
    public function store(StoreVendorProfileRequest $request): JsonResponse
    {
        $this->authorize('admin');
        try {
            $profile = VendorProfile::create(array_merge(
                $request->validated(),
                ['created_by_user_id' => $request->user()->id],
            ));

            $profile->loadCount('columnMappings');

            return response()->json($profile, 201);
        } catch (UniqueConstraintViolationException) {
            return response()->json([
                'message' => 'A vendor profile with this name already exists.',
                'errors'  => ['name' => ['A vendor profile with this name already exists.']],
            ], 409);
        }
    }

    /**
     * Update an existing vendor profile (partial updates supported).
     *
     * PUT/PATCH /api/v1/vendor-profiles/{vendor_profile}
     */
    public function update(UpdateVendorProfileRequest $request, VendorProfile $vendorProfile): JsonResponse
    {
        $this->authorize('admin');
        try {
            $vendorProfile->update($request->validated());
            $vendorProfile->loadCount('columnMappings');

            return response()->json($vendorProfile);
        } catch (UniqueConstraintViolationException) {
            return response()->json([
                'message' => 'A vendor profile with this name already exists.',
                'errors'  => ['name' => ['A vendor profile with this name already exists.']],
            ], 409);
        }
    }

    /**
     * Delete a vendor profile. CASCADE on column_mappings is handled at the
     * database level (the migration defines ON DELETE CASCADE).
     *
     * DELETE /api/v1/vendor-profiles/{vendor_profile}
     */
    public function destroy(VendorProfile $vendorProfile): JsonResponse
    {
        $this->authorize('admin');
        $vendorProfile->delete();

        return response()->json(null, 204);
    }
}
