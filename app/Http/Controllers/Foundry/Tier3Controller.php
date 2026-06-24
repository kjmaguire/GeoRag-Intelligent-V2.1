<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/Tier3Controller — request flow for jurisdiction-gated public
 * geoscience layers. Writes requests to silver.tier3_unlock_requests.
 */
class Tier3Controller extends Controller
{
    public function show(Request $request): Response
    {
        $user = $request->user();
        $isAdmin = (bool) ($user->is_admin ?? false);
        // 2026-06-03 audit: `$user->workspace_id` is null (no column) so
        // this always defaulted to empty string → the "latest request"
        // lookup matched nothing and the Inertia page rendered "no
        // request" even when the user had pending tier3 unlocks in
        // their actual workspace. Resolve from the user's first project.
        $wsId = (string) ($user->projects()
            ->value('silver.projects.workspace_id') ?? '');

        $layers = [
            ['layer_id' => 'petroleum_wells', 'label' => 'Petroleum wells', 'jurisdictions' => ['AB', 'BC', 'SK'], 'license' => 'Restricted-attribution', 'row_count_estimate' => '612k wells'],
            ['layer_id' => 'petroleum_well_trajectories', 'label' => 'Well trajectories', 'jurisdictions' => ['AB', 'BC'], 'license' => 'Restricted-attribution', 'row_count_estimate' => '412k trajectories'],
            ['layer_id' => 'petroleum_pools', 'label' => 'Petroleum pools', 'jurisdictions' => ['AB'], 'license' => 'Restricted-attribution', 'row_count_estimate' => '4,820 pools'],
            ['layer_id' => 'geochronology_samples', 'label' => 'Geochronology samples', 'jurisdictions' => ['CA-wide'], 'license' => 'Attribution-required', 'row_count_estimate' => '12.4k samples'],
            ['layer_id' => 'geochemistry_samples', 'label' => 'Geochemistry samples', 'jurisdictions' => ['CA-wide'], 'license' => 'Attribution-required', 'row_count_estimate' => '284k samples'],
            ['layer_id' => 'geological_feature_points', 'label' => 'Feature points (compiled)', 'jurisdictions' => ['CA-wide'], 'license' => 'Attribution-required', 'row_count_estimate' => '48k points'],
            ['layer_id' => 'geological_feature_lines', 'label' => 'Feature lines (compiled)', 'jurisdictions' => ['CA-wide'], 'license' => 'Attribution-required', 'row_count_estimate' => '24k lines'],
        ];

        $latest = null;
        if ($wsId !== '') {
            try {
                $latest = DB::table('silver.tier3_unlock_requests')
                    ->where('workspace_id', $wsId)
                    ->orderByDesc('created_at')
                    ->first();
            } catch (\Throwable $e) {
                // table may not exist in some envs yet
            }
        }

        return Inertia::render('Foundry/Tier3Unlock', [
            'workspace_id' => $wsId,
            'layers' => $layers,
            'request_status' => $latest->status ?? 'none',
            'can_approve' => $isAdmin,
            'empty' => false,
        ]);
    }

    public function request(Request $request): RedirectResponse
    {
        $user = $request->user();
        $data = $request->validate([
            'layer_ids' => 'array',
            'layer_ids.*' => 'string|max:100',
            'attest_purpose' => 'boolean',
            'attest_retention' => 'boolean',
            'attest_attribution' => 'boolean',
        ]);

        // 2026-06-03 audit: resolve real workspace_id from the user's
        // first project (User has no workspace_id column, so the
        // previous `?? all-zeros` fallback always fired, and the row
        // was effectively invisible — the admin review queue filters
        // by workspace_id and never matched all-zeros). Refuse the
        // insert when no real workspace can be resolved rather than
        // silently writing a row no one will see.
        $resolvedWorkspaceId = $user->projects()
            ->value('silver.projects.workspace_id');
        if ($resolvedWorkspaceId === null) {
            return back()->with('error', 'Unable to resolve your workspace; tier3 request not submitted.');
        }

        try {
            DB::table('silver.tier3_unlock_requests')->insert([
                'workspace_id' => $resolvedWorkspaceId,
                'requested_by' => $user->id,
                'layer_ids' => '{'.implode(',', array_map(fn ($s) => '"'.addslashes((string) $s).'"', $data['layer_ids'] ?? [])).'}',
                'attest_purpose' => (bool) ($data['attest_purpose'] ?? false),
                'attest_retention' => (bool) ($data['attest_retention'] ?? false),
                'attest_attribution' => (bool) ($data['attest_attribution'] ?? false),
                'status' => 'pending',
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        } catch (\Throwable $e) {
            return back()->with('flash', 'Tier 3 request could not be saved: '.$e->getMessage());
        }

        return back()->with('flash', 'Tier 3 unlock request recorded (pending admin review).');
    }
}
