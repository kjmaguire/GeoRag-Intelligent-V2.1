<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Inertia\Inertia;
use Inertia\Response;

/**
 * /admin/dashboards — cross-links to the 16 Grafana dashboards.
 *
 * The dashboards live in Grafana JSON (docker/grafana/dashboards/*.json).
 * This page is just an index — clicking through opens the dashboard in
 * the embedded Grafana iframe (config: GRAFANA_BASE_URL) OR in a new
 * tab when the operator prefers.
 */
class DashboardsController extends Controller
{
    /**
     * Catalogue of dashboards shipped in docker/grafana/dashboards/.
     * Each entry: { slug, uid, title, audience, description }.
     * `uid` matches the Grafana dashboard JSON's `uid` field.
     */
    private const CATALOG = [
        // §16.3 ops-tier (13)
        ['slug' => 'overview',                  'uid' => 'georag-overview',                  'title' => 'GeoRAG Overview',                'audience' => 'ops',     'description' => 'High-level FastAPI + Laravel health.'],
        ['slug' => 'services',                  'uid' => 'georag-services',                  'title' => 'Services',                       'audience' => 'ops',     'description' => 'Per-service latency + error rates.'],
        ['slug' => 'signals',                   'uid' => 'georag-signals',                   'title' => 'Signals',                        'audience' => 'ops',     'description' => 'Cross-cutting RED + USE metrics.'],
        ['slug' => 'rag-quality',               'uid' => 'georag-rag-quality',               'title' => 'RAG Quality',                    'audience' => 'ops',     'description' => 'Hallucination drops + citation pass-rate.'],
        ['slug' => 'authz',                     'uid' => 'georag-authz',                     'title' => 'AuthZ',                          'audience' => 'ops',     'description' => 'Auth + tenant-isolation events.'],
        ['slug' => 'laravel-queue',             'uid' => 'georag-laravel-queue',             'title' => 'Laravel Queue / Horizon',        'audience' => 'ops',     'description' => 'Horizon supervisor + job throughput.'],
        ['slug' => 'integrations',              'uid' => 'georag-integrations',              'title' => 'Integrations',                   'audience' => 'ops',     'description' => 'Kestra + Kestra + webhook senders.'],
        ['slug' => 'workflows-hatchet',         'uid' => 'georag-workflows-hatchet',         'title' => 'Hatchet Workflows',              'audience' => 'ops',     'description' => 'Hatchet worker pools + workflow runs.'],
        ['slug' => 'workflows-dagster',         'uid' => 'georag-workflows-dagster',         'title' => 'Dagster Workflows',              'audience' => 'ops',     'description' => 'Dagster asset materialisation lag.'],
        ['slug' => 'workflows-kestra',          'uid' => 'georag-workflows-kestra',          'title' => 'Kestra Workflows',               'audience' => 'ops',     'description' => 'Kestra flow execution telemetry.'],
        ['slug' => 'workflows-llm-pipeline',    'uid' => 'georag-workflows-llm-pipeline',    'title' => 'LLM Pipeline',                   'audience' => 'ops',     'description' => 'vLLM + Anthropic fallback latency + tokens.'],
        ['slug' => 'workflows-cost-burn',       'uid' => 'georag-workflows-cost-burn',       'title' => 'Cost Burn',                      'audience' => 'ops',     'description' => 'Per-workflow token spend.'],
        ['slug' => 'workflows-outbox',          'uid' => 'georag-workflows-outbox',          'title' => 'Outbox Dispatcher',              'audience' => 'ops',     'description' => 'Per-target store delivery rate + dead-letter.'],

        // §16.1 product-tier (3 starters)
        ['slug' => 'product-workspace-health',     'uid' => 'georag-product-workspace-health',     'title' => 'Workspace Health',         'audience' => 'product', 'description' => 'Per-workspace ingestion + queries + citation pass-rate.'],
        ['slug' => 'product-citation-quality',     'uid' => 'georag-product-citation-quality',     'title' => 'Citation Quality',         'audience' => 'product', 'description' => 'Per-workspace §04i layer drops + conflicts disclosed.'],
        ['slug' => 'product-ingestion-throughput', 'uid' => 'georag-product-ingestion-throughput', 'title' => 'Ingestion Throughput',     'audience' => 'product', 'description' => 'Bytes/sec by format + OCR fallback rate.'],
    ];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $grafanaBase = rtrim(
            config('services.grafana.base_url')
                ?? env('GRAFANA_BASE_URL', 'http://localhost:3000'),
            '/',
        );

        return Inertia::render('Admin/Dashboards', [
            'grafana_base_url' => $grafanaBase,
            'dashboards' => self::CATALOG,
        ]);
    }
}
