<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;

/**
 * Internal — FastAPI / Dagster → Laravel bridge for admin surface push events.
 *
 * Service-key auth only. Dispatches the generic
 * {@see App\Events\Admin\AdminSurfaceUpdated} event on the
 * surface-specific admin private channel; the receiving SPA page filters
 * by `surface` and calls router.reload({ only: affected_props }).
 *
 * Sibling endpoints:
 *   - /api/internal/v1/ingest-progress/broadcast    — workspace-scoped ingestion
 *   - /api/internal/v1/workspace-data-updated       — workspace-scoped non-ingestion
 *   - /api/internal/admin/reports/{build_id}/progress — admin per-build cockpit
 *
 * This is the catch-all admin-side bridge for the workflows that don't
 * have a domain-specific endpoint.
 *
 * Surface allow-list mirrors the channel registrations in
 * routes/channels.php — any new surface needs the matching
 * Broadcast::channel() registration before requests will reach a
 * subscriber.
 */
class AdminSurfaceUpdatedBridgeController extends Controller
{
    /**
     * Allow-listed surface discriminators. Must match the channel
     * registrations in routes/channels.php (without the leading
     * `admin.` prefix). Rejecting unknown surfaces here is the
     * server-side safety net so a typo in a workflow doesn't silently
     * broadcast into a dead channel.
     */
    private const ALLOWED_SURFACES = [
        'workflow-runs',
        'cluster-ingest',
        'target-recommendation',
        'target-run',
        'reports',
        'ml-training',
        'audit-findings',
        'alerts-inbox',
        'ingestion-review',
        // Phase 3 additions — admin-gated Foundry pages that share the
        // Phase 2 broadcast infrastructure instead of using a workspace
        // channel. Foundry/SupportCockpit and Dashboards/LlmCost are
        // both admin-only in practice (LlmCost shows global usage data,
        // SupportCockpit aborts unless is_admin).
        'support-cockpit',
        'llm-cost',
        // Phase 5 additions — the remaining background admin surfaces.
        // Cache-telemetry stays allow-listed without a wired page (decision
        // 6 deferred live updates) so a future tightening doesn't have to
        // edit two files. All others have a subscribing admin page.
        'cache-telemetry',
        'eval-dashboard',
        'conflicts',
        'audit-explorer',
        'backups',
        'integrations',
        'export-gate',
        'decision-history',
        'hypothesis-workspace',
        'what-changed',
        'source-trust',
        // Phase 6 — closeout for the 2 Dashboards pages without a natural
        // existing-surface fit (EvidenceQuality reads silver.answer_runs
        // rejection stats; VisualReadiness reads MV-refreshed viz coverage).
        // The other 3 Dashboards pages (PublicGeoOverlay, TargetRecommendation,
        // Reporting) reuse existing surfaces — see channels.php notes.
        'dashboards-evidence-quality',
        'dashboards-visual-readiness',
    ];

    public function broadcast(Request $request): JsonResponse
    {
        $payload = $request->validate([
            'surface' => ['required', 'string', 'in:'.implode(',', self::ALLOWED_SURFACES)],
            'surface_id' => ['nullable', 'string', 'max:128'],
            'affected_props' => ['required', 'array', 'min:1'],
            'affected_props.*' => ['string', 'max:60'],
            'payload' => ['nullable', 'array'],
        ]);

        AdminSurfaceUpdated::dispatch(
            $payload['surface'],
            $payload['surface_id'] ?? null,
            $payload['affected_props'],
            $payload['payload'] ?? [],
        );

        Log::info('admin.surface_updated.broadcast', [
            'surface' => $payload['surface'],
            'surface_id' => $payload['surface_id'] ?? null,
            'affected_props' => $payload['affected_props'],
            'payload_keys' => array_keys($payload['payload'] ?? []),
        ]);

        return response()->json(['ok' => true]);
    }
}
