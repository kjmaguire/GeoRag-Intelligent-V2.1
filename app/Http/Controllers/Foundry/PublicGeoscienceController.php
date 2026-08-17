<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/PublicGeoscienceController — standalone "Public Geo" browse page.
 *
 * Lands at /public-geoscience, linked from the top ORG nav bar. Not
 * project-scoped: public_geo data isn't tenant data (same reasoning as
 * PublicGeoscienceMapController, which this page's frontend calls directly
 * client-side via GET /api/v1/public-geoscience/map).
 *
 * 2026-08-17 — added after the org-nav "Public Geo" link was found pointing
 * nowhere (the old /foundry/public-geoscience destination was deleted in
 * the reader-core trim along with everything else Martin-tile-based). This
 * gives that nav entry a real page instead of restoring the dead Martin
 * proxy path. See PublicGeoscienceMapController's docblock for the data
 * scope (4 point-geometry tables; polygons out of scope).
 */
class PublicGeoscienceController extends Controller
{
    public function show(): Response
    {
        return Inertia::render('Foundry/PublicGeoscience');
    }
}
