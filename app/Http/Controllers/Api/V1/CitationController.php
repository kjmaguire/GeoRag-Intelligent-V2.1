<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\Citations\CitationResolverRegistry;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * Citation source lookup — resolves a `source_chunk_id` to the underlying
 * source text, section, and provenance metadata.
 *
 * Used by the Document Viewer to display the exact source content that a
 * citation refers to, enabling QP-level verification of RAG answers.
 *
 * Routes:
 *   GET /api/v1/citations/resolve?source_chunk_id=...&citation_type=...
 *
 * Architecture
 * ------------
 * The controller is intentionally thin — it delegates dispatch to
 * `CitationResolverRegistry`, which maps each `source_chunk_id` prefix to a
 * dedicated `CitationResolver` implementation. This refactor (2026-05-07)
 * replaced an 11-branch `if (str_starts_with(...))` chain with a strategy
 * pattern; adding a new source type is now:
 *
 *   1. Add a new class in `app/Services/Citations/Resolvers/`.
 *   2. Register it in `App\Providers\CitationResolverServiceProvider`.
 *
 * No edit to this controller. No edit to the dispatcher.
 *
 * Supported source_chunk_id prefixes
 * ----------------------------------
 *   silver.collars:count=20:first=...
 *   silver.lithology_logs:hole=PLS-20-01:collar=...:intervals=4
 *   silver.samples:element=U3O8_ppm:count=25
 *   georag_reports:44a67709-...:section=13:chunk=...
 *   pg_mine:CA-SK-MINE-LOC:feature=12345:pg_id=<uuid>
 *   pg_mineral_occurrence:CA-SK-SMDI:feature=7788:pg_id=<uuid>
 *   pg_drillhole_collar:CA-SK-DRILLHOLE:feature=9001:pg_id=<uuid>
 *   pg_resource_potential_zone:CA-SK-RESOURCE-POTENTIAL-GOLD:feature=...:pg_id=<uuid>
 *   pg_rock_sample:CA-SK-ROCK-SAMPLE:feature=...:pg_id=<uuid>
 *   pg_assessment_survey:CA-SK-SMAD:feature=...:pg_id=<uuid>
 *   pg_mineral_disposition:CA-SK-MINERAL-DISPOSITION:feature=...:pg_id=<uuid>
 */
final class CitationController extends Controller
{
    use SetsWorkspaceRlsContext;

    /**
     * Sentinel workspace for users with no project memberships. Matches no
     * row in any tenant table (workspace_id is a real workspace UUID or, at
     * worst, NULL) so tenant-scoped resolvers fail CLOSED while
     * workspace-global resolvers (public geoscience) still work.
     */
    private const NIL_WORKSPACE_ID = '00000000-0000-0000-0000-000000000000';

    public function __construct(
        private readonly CitationResolverRegistry $registry,
    ) {}

    /**
     * Resolve a source_chunk_id to its original content.
     *
     * Security fix 2026-08-14 (HIGH — cross-tenant IDOR): resolution now runs
     * once per workspace the authenticated user can access (via the
     * project_user pivot → silver.projects.workspace_id), inside
     * {@see SetsWorkspaceRlsContext::withWorkspaceRls()} so the
     * `app.workspace_id` GUC removes the fail-open NULL-GUC RLS fallback.
     * Tenant-scoped resolvers additionally apply an explicit
     * `workspace_id = ?` filter (belt and braces — never rely on fail-open
     * RLS alone). A record that exists in a workspace the caller cannot
     * access resolves exactly like a record that does not exist: 404. The
     * same 404 is returned for genuinely missing records so the endpoint is
     * not an existence oracle.
     *
     * Returns 200 for resolved records and for unknown prefixes (structured
     * "not recognised" payload — the citation viewer renders the gap).
     * Returns 400 only when the required query parameter is missing.
     */
    public function resolve(Request $request): JsonResponse
    {
        $sourceId = (string) $request->query('source_chunk_id', '');

        if ($sourceId === '') {
            return response()->json(
                ['message' => 'source_chunk_id is required'],
                400,
            );
        }

        $workspaceIds = $this->accessibleWorkspaceIds($request);

        // Try each accessible workspace; a hit returns immediately. A user is
        // almost always in exactly one workspace, so this loop is one pass in
        // practice.
        $resolved = null;
        foreach ($workspaceIds as $workspaceId) {
            $resolved = $this->withWorkspaceRls(
                $workspaceId,
                fn (): ?JsonResponse => $this->registry->resolve($sourceId, $workspaceId),
            );

            if ($resolved === null) {
                // Unknown prefix — workspace-independent; stop looping.
                break;
            }

            if ($resolved->getStatusCode() !== 404) {
                return $resolved;
            }
        }

        if ($resolved !== null) {
            // 404 in every accessible workspace: not found OR cross-tenant —
            // indistinguishable by design (no existence oracle).
            return $resolved;
        }

        // Unknown prefix — return a structured "not recognised" payload so
        // the citation viewer can render a helpful empty state.
        return response()->json([
            'source_type' => 'unknown',
            'source_chunk_id' => $sourceId,
            'text' => 'Source type not recognized.',
            'metadata' => [],
        ]);
    }

    /**
     * Workspaces the authenticated user may read, derived from the same
     * project_user membership pivot the other Api/V1 controllers gate on
     * (User::hasProjectAccess). Falls back to a nil sentinel when the user
     * has no memberships so tenant lookups match nothing (fail CLOSED).
     *
     * @return list<string>
     */
    private function accessibleWorkspaceIds(Request $request): array
    {
        $workspaceIds = $request->user()
            ->projects()
            ->pluck('silver.projects.workspace_id')
            ->filter()
            ->map(fn ($id): string => (string) $id)
            ->unique()
            ->values()
            ->all();

        return $workspaceIds === [] ? [self::NIL_WORKSPACE_ID] : $workspaceIds;
    }
}
