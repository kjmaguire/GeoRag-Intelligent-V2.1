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
 *   GET /api/v1/citations/resolve?source_chunk_id=...&citation_type=...[&project_id=...]
 *
 * `project_id` is optional. When present, resolution is limited to that
 * project (the caller must be a member); when absent, to the projects the
 * caller belongs to. Either way another project's records resolve as 404.
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
 *   silver.collars:hole=PLS-20-01:collar=<uuid>:assays=12:litho=4
 *   silver.collars:miss
 *   silver.lithology_logs:hole=PLS-20-01:collar=...:intervals=4
 *   silver.samples:element=U3O8_ppm:count=25
 *   silver.assays_v2:assay_id=<uuid>
 *   georag_reports:44a67709-...:section=13:chunk=...
 *   georag_reports:None:section=unknown:chunk=<passage uuid>   (ADR-0012 summary)
 *   silver.projects:slug=<slug>:company=...:curves=3:reports=12
 *   silver.project_summary:project=<uuid>:rows=8:first_row=...
 *   silver.coverage_gap:project=<uuid>:indexed=40:processed=31:attrs=5
 *   silver.drill_traces:project=<uuid>:holes=12:first_collar=<uuid>:hole_filter=all
 *   gold.structure_measurements_visual:project=<uuid>:points=57:first=...
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

        // workspace_id => the project ids inside it that this request may read.
        // Workspace scope alone let a member of project A resolve project B's
        // chunks whenever both lived in one workspace; the project is the
        // authorisation unit everywhere else in Api/V1 (hasProjectAccess).
        $projectId = $request->query('project_id');
        $scopes = $this->accessibleScopes($request, is_string($projectId) && $projectId !== '' ? $projectId : null);

        // Try each accessible workspace; a hit returns immediately. A user is
        // almost always in exactly one workspace, so this loop is one pass in
        // practice.
        $resolved = null;
        foreach ($scopes as $workspaceId => $projectIds) {
            $workspaceId = (string) $workspaceId;
            $resolved = $this->withWorkspaceRls(
                $workspaceId,
                fn (): ?JsonResponse => $this->registry->resolve($sourceId, $workspaceId, $projectIds),
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
     * The (workspace, projects) pairs the request may read, derived from the
     * same project_user membership pivot the other Api/V1 controllers gate on
     * (User::hasProjectAccess).
     *
     * With an explicit `project_id` the scope narrows to that one project, and
     * a project the caller is not a member of yields a scope that matches
     * nothing (so the answer is the same 404 as a missing record). Without
     * one, every project the caller belongs to is in scope, which keeps the
     * existing callers working while still excluding other projects'
     * records. Falls back to a nil sentinel when there is nothing to scope to
     * so tenant lookups match nothing (fail CLOSED).
     *
     * @return array<string, list<string>> workspace_id => project_ids
     */
    private function accessibleScopes(Request $request, ?string $onlyProjectId): array
    {
        $scopes = [];
        $rows = $request->user()
            ->projects()
            ->get(['silver.projects.project_id', 'silver.projects.workspace_id']);

        foreach ($rows as $project) {
            if ($project->workspace_id === null) {
                continue;
            }
            if ($onlyProjectId !== null && strcasecmp((string) $project->project_id, $onlyProjectId) !== 0) {
                continue;
            }
            $scopes[(string) $project->workspace_id][] = (string) $project->project_id;
        }

        return $scopes === [] ? [self::NIL_WORKSPACE_ID => []] : $scopes;
    }
}
