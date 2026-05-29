<?php

declare(strict_types=1);

use App\Events\Admin\AdminSurfaceUpdated;
use App\Models\QueryAuditLog;
use Illuminate\Support\Facades\Broadcast;

Broadcast::channel('App.Models.User.{id}', function ($user, $id) {
    return (int) $user->id === (int) $id;
});

/**
 * Private channel for streaming RAG query results.
 *
 * Channel name: query.{queryId}
 *
 * Authorization gate (tenant isolation — see A1 fix):
 *   1. Channel name must be a well-formed UUID.
 *   2. A QueryAuditLog row with that query_id must exist.
 *   3. The authenticated user must be the row's owner (user_id match).
 *   4. The user must STILL have access to the row's project_id at subscribe
 *      time (handles revocations between query submit and subscribe).
 *
 * Returning false denies the subscription without leaking existence.
 */
Broadcast::channel('query.{queryId}', function ($user, string $queryId) {
    if ($user === null) {
        return false;
    }

    if (! preg_match(
        '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
        $queryId,
    )) {
        return false;
    }

    $row = QueryAuditLog::where('query_id', $queryId)->first();

    if ($row === null) {
        return false;
    }

    if ((int) $row->user_id !== (int) $user->id) {
        return false;
    }

    if ($row->project_id === null || ! $user->hasProjectAccess($row->project_id)) {
        return false;
    }

    return true;
});

/**
 * Dashboard — workspace activity feed (spec §6).
 * User must have at least one project in the workspace.
 */
Broadcast::channel('workspace.{workspaceId}.activity', function ($user, string $workspaceId) {
    // TODO: Validate $workspaceId when the Workspace model is introduced.
    // Currently workspace = "all projects the user can access" (no workspace model yet).
    return $user->projects()->exists();
});

/**
 * Dashboard — document ingestion stage transitions (spec §6).
 * User must have access to the project.
 */
Broadcast::channel('project.{projectId}.ingestion', function ($user, string $projectId) {
    return $user->hasProjectAccess($projectId);
});

/**
 * Master-plan §3 Step 8 — Silver Review queue live broadcast
 * (doc-phase 64). Admins viewing /admin/ingestion-review subscribe
 * here; receives IngestionReviewDispositionChanged events when any
 * admin applies a disposition.
 *
 * Auth: admin Gate (users.is_admin = true) — multi-operator queue
 * coordination is an admin surface only.
 */
Broadcast::channel('admin.ingestion-review', function ($user) {
    return (bool) ($user->is_admin ?? false);
});

/**
 * Phase H4 §7 — real-time generate_report progress per build.
 * Channel: private-admin.reports.{build_id}
 * Auth: admin Gate only.
 */
Broadcast::channel('admin.reports.{build_id}', function ($user, string $build_id) {
    if (! preg_match(
        '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
        $build_id,
    )) {
        return false;
    }

    return (bool) ($user->is_admin ?? false);
});

/**
 * Phase 2 of the real-time staleness fix — generic admin surface channels.
 *
 * All admin-gated. The {@see AdminSurfaceUpdated} event
 * fires on these whenever the underlying data table gets a relevant
 * write. The receiving page calls router.reload({ only: [...] }) using
 * the event's affected_props list. See the event class docblock for
 * the channel naming convention.
 *
 * Per-resource channel `admin.target-run.{run_id}` matches the
 * precedent set by `admin.reports.{build_id}` (cockpit-style
 * drilldown). The shared list-page channels (workflow-runs, reports,
 * ml-training, etc.) match the `admin.ingestion-review` precedent.
 */
$adminOnly = static fn ($user) => (bool) ($user->is_admin ?? false);

Broadcast::channel('admin.workflow-runs', $adminOnly);
Broadcast::channel('admin.cluster-ingest', $adminOnly);
Broadcast::channel('admin.target-recommendation', $adminOnly);
Broadcast::channel('admin.reports', $adminOnly);
Broadcast::channel('admin.ml-training', $adminOnly);
Broadcast::channel('admin.audit-findings', $adminOnly);
Broadcast::channel('admin.alerts-inbox', $adminOnly);
// Phase 3 — Foundry/SupportCockpit and Dashboards/LlmCost ride the
// admin broadcast plumbing (both are admin-gated in practice; reuses
// the Phase 2 AdminSurfaceUpdated event + ALLOWED_SURFACES allow-list).
Broadcast::channel('admin.support-cockpit', $adminOnly);
Broadcast::channel('admin.llm-cost', $adminOnly);
// Phase 5 — remaining low-frequency background admin surfaces.
// Same AdminSurfaceUpdated event + useAdminSurfaceUpdated hook;
// just more channel registrations matching ALLOWED_SURFACES entries.
// admin.cache-telemetry is registered for symmetry but has no
// subscriber yet (live updates deferred per Phase 5 decision 6).
Broadcast::channel('admin.cache-telemetry', $adminOnly);
Broadcast::channel('admin.eval-dashboard', $adminOnly);
Broadcast::channel('admin.conflicts', $adminOnly);
Broadcast::channel('admin.audit-explorer', $adminOnly);
Broadcast::channel('admin.backups', $adminOnly);
Broadcast::channel('admin.integrations', $adminOnly);
Broadcast::channel('admin.export-gate', $adminOnly);
Broadcast::channel('admin.decision-history', $adminOnly);
Broadcast::channel('admin.hypothesis-workspace', $adminOnly);
Broadcast::channel('admin.what-changed', $adminOnly);
Broadcast::channel('admin.source-trust', $adminOnly);
// Phase 6 closeout — 2 Dashboards pages without a natural existing-surface
// fit. (EvidenceQuality reads silver.answer_runs; VisualReadiness reads
// MV-refreshed viz coverage.) The other 3 Dashboards pages reuse existing
// channels: PublicGeoOverlay → public-geoscience.tiles,
// TargetRecommendation → admin.target-recommendation, Reporting → admin.reports.
Broadcast::channel('admin.dashboards-evidence-quality', $adminOnly);
Broadcast::channel('admin.dashboards-visual-readiness', $adminOnly);

Broadcast::channel('admin.target-run.{run_id}', function ($user, string $run_id) {
    if (! preg_match(
        '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
        $run_id,
    )) {
        return false;
    }

    return (bool) ($user->is_admin ?? false);
});

/**
 * Phase 4 — Public-Geoscience tile cache invalidation channel.
 *
 * Fires PublicGeoscienceTilesInvalidated when the public_geoscience_pull
 * workflow (or future SMDI pipeline) successfully refreshes public_geo.*
 * data. Subscribers (PublicGeoscienceMap) drop their MapLibre tile cache
 * via setTiles() + new ?v={epoch} cache-bust.
 *
 * Auth: any authenticated user. Matches the route-level auth on
 * /tiles/public-geoscience/* (also Sanctum-only) — PGEO is a workspace-
 * global read-only corpus, no per-project scoping.
 */
Broadcast::channel('public-geoscience.tiles', static fn ($user) => $user !== null);
