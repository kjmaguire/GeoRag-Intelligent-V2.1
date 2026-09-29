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
 * scope (4 point-geometry tables always; the 4 polygon tables on request via
 * `layers=` since 2026-09-29).
 *
 * The page also carries an admin-only "Sync now" control and a freshness
 * line, both served by PublicGeoscienceSyncController; the admin flag comes
 * from the shared `auth.user.is_admin` Inertia prop, and the POST is
 * re-authorised server-side against the `admin` gate.
 */
class PublicGeoscienceController extends Controller
{
    public function show(): Response
    {
        return Inertia::render('Foundry/PublicGeoscience');
    }
}
