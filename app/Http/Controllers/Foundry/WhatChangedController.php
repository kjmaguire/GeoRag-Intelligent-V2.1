<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/WhatChangedController — §9.13 What Changed Detector.
 *
 * Composes a feed from existing tables: bronze_ingest_manifest (new ingests),
 * silver.hypotheses (status flips), audit.query_audit_log (retrieval drift),
 * silver.collaboration_audit_log (decisions / promotions).
 */
class WhatChangedController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $since = CarbonImmutable::now()->subDays(7);
        $events = collect();

        // 1) New ingestions reaching silver
        try {
            $ingests = DB::table('bronze.ingest_manifest')
                ->where('project_id', $project->project_id)
                ->where('created_at', '>=', $since)
                ->orderByDesc('created_at')
                ->limit(20)
                ->get();
            foreach ($ingests as $r) {
                $events->push([
                    'id' => 'ing-' . ($r->manifest_id ?? $r->id ?? uniqid()),
                    'timestamp_seconds_ago' => CarbonImmutable::parse((string) $r->created_at)->diffInSeconds(),
                    'group' => self::groupFor((string) $r->created_at),
                    'kind' => 'ingestion',
                    'priority' => 'med',
                    'title' => 'Ingest manifest committed',
                    'detail' => (string) ($r->source_name ?? $r->manifest_id ?? 'unknown source'),
                    'refs' => [(string) ($r->source_name ?? '')],
                    'impacted' => ['data'],
                ]);
            }
        } catch (\Throwable $e) {
            // table may not exist in all envs
        }

        // 2) Hypothesis flips
        try {
            $hypos = DB::table('silver.hypotheses')
                ->where('project_id', $project->project_id)
                ->where('updated_at', '>=', $since)
                ->orderByDesc('updated_at')
                ->limit(20)
                ->get();
            foreach ($hypos as $h) {
                $events->push([
                    'id' => 'hyp-' . ($h->hypothesis_id ?? uniqid()),
                    'timestamp_seconds_ago' => CarbonImmutable::parse((string) $h->updated_at)->diffInSeconds(),
                    'group' => self::groupFor((string) $h->updated_at),
                    'kind' => 'hypothesis_flip',
                    'priority' => 'high',
                    'title' => 'Hypothesis status update',
                    'detail' => (string) ($h->title ?? $h->hypothesis_id ?? ''),
                    'refs' => [(string) ($h->hypothesis_id ?? '')],
                    'impacted' => ['reasoning'],
                ]);
            }
        } catch (\Throwable $e) {
            // table may not exist
        }

        // 3) Recent decisions
        try {
            $decisions = DB::table('silver.decision_records')
                ->where('project_id', $project->project_id)
                ->where('created_at', '>=', $since)
                ->orderByDesc('created_at')
                ->limit(20)
                ->get();
            foreach ($decisions as $d) {
                $events->push([
                    'id' => 'dec-' . ($d->decision_id ?? uniqid()),
                    'timestamp_seconds_ago' => CarbonImmutable::parse((string) $d->created_at)->diffInSeconds(),
                    'group' => self::groupFor((string) $d->created_at),
                    'kind' => 'decision_logged',
                    'priority' => 'low',
                    'title' => 'Decision recorded',
                    'detail' => (string) ($d->title ?? $d->decision_id ?? ''),
                    'refs' => [(string) ($d->decision_id ?? '')],
                    'impacted' => ['audit'],
                ]);
            }
        } catch (\Throwable $e) {
            // table may not exist
        }

        $sorted = $events->sortBy('timestamp_seconds_ago')->values();

        return Inertia::render('Foundry/WhatChangedFeed', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'events' => $sorted,
            'empty' => $sorted->isEmpty(),
        ]);
    }

    private static function groupFor(string $isoTs): string
    {
        try {
            $t = CarbonImmutable::parse($isoTs);
            $diffHours = $t->diffInHours();
            if ($diffHours < 24) return 'today';
            if ($diffHours < 48) return 'yesterday';
            if ($diffHours < 168) return 'this week';
            return 'older';
        } catch (\Throwable $e) {
            return 'older';
        }
    }
}
