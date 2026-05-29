<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\SavedMapView;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Throwable;

/**
 * Saved map views CRUD endpoints (§6.5 doc-phase 105 skeleton).
 *
 * Per-user, per-project, workspace-scoped MapLibre saved views.
 * RLS at the database level (silver.saved_map_views policy) enforces
 * workspace isolation; this controller adds user-scope on top (a
 * user can only see/edit their own views within the workspace,
 * unless `is_shared=true` flag — future v2).
 *
 * Doc-phase 105 — skeleton. Live behavior lands when:
 * - the MapLibre frontend (§6.7+) exists to call these endpoints,
 * - the workspace_id resolution middleware sets `app.workspace_id`
 *   for RLS scoping per request.
 */
class SavedMapViewController extends Controller
{
    /**
     * List saved map views for the authenticated user + project.
     *
     * GET /api/v1/projects/{project}/saved-map-views
     */
    public function index(Request $request, string $projectId): JsonResponse
    {
        throw new \LogicException(
            'SavedMapViewController::index is a doc-phase 105 skeleton.'
        );
    }

    /**
     * Create a new saved map view.
     *
     * POST /api/v1/projects/{project}/saved-map-views
     */
    public function store(Request $request, string $projectId): JsonResponse
    {
        throw new \LogicException(
            'SavedMapViewController::store is a doc-phase 105 skeleton.'
        );
    }

    /**
     * Show a single saved map view.
     *
     * GET /api/v1/projects/{project}/saved-map-views/{view}
     */
    public function show(Request $request, string $projectId, string $viewId): JsonResponse
    {
        throw new \LogicException(
            'SavedMapViewController::show is a doc-phase 105 skeleton.'
        );
    }

    /**
     * Update an existing saved map view (rename / re-camera).
     *
     * PATCH /api/v1/projects/{project}/saved-map-views/{view}
     */
    public function update(Request $request, string $projectId, string $viewId): JsonResponse
    {
        throw new \LogicException(
            'SavedMapViewController::update is a doc-phase 105 skeleton.'
        );
    }

    /**
     * Delete a saved map view.
     *
     * DELETE /api/v1/projects/{project}/saved-map-views/{view}
     */
    public function destroy(Request $request, string $projectId, string $viewId): JsonResponse
    {
        throw new \LogicException(
            'SavedMapViewController::destroy is a doc-phase 105 skeleton.'
        );
    }
}
