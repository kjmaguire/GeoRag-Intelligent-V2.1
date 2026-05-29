<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Inertia\Testing\AssertableInertia;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Smoke test: every Foundry route resolves to a 200 OK Inertia response for
 * an authenticated user, and the Inertia page component name matches what
 * the resolver in resources/js/app.tsx expects.
 *
 * Two test phases:
 *   1. Org-scoped routes (no project slug)
 *   2. Project-scoped routes — only run if a project exists in the DB
 */
final class FoundryRoutesSmokeTest extends TestCase
{
    public static function orgRoutes(): array
    {
        return [
            'portfolio' => ['/dashboard', 'Foundry/Portfolio'],
            'projects' => ['/projects', 'Foundry/Projects'],
            'inbox' => ['/inbox', 'Foundry/Inbox'],
            'settings' => ['/settings', 'Foundry/Settings'],
            'tier3' => ['/public-geoscience/tier3-unlock', 'Foundry/Tier3Unlock'],
            'pgeo' => ['/foundry/public-geoscience', 'Foundry/PublicGeo'],
            'imports' => ['/foundry/imports/wizard', 'Foundry/DataImportWizard'],
            'newproject' => ['/foundry/projects/new', 'Foundry/NewProject'],
        ];
    }

    #[DataProvider('orgRoutes')]
    public function test_org_route_renders_for_authenticated_user(string $url, string $expectedComponent): void
    {
        $user = User::factory()->create();

        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component($expectedComponent));
    }

    public function test_login_page_renders_unauthenticated(): void
    {
        $response = $this->get('/foundry/login');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component('Foundry/Login'));
    }

    public function test_support_cockpit_requires_admin(): void
    {
        $user = User::factory()->create(['is_admin' => false]);

        $response = $this->actingAs($user)->get('/support-cockpit');

        $response->assertStatus(403);
    }

    public function test_support_cockpit_renders_for_admin(): void
    {
        $user = User::factory()->create(['is_admin' => true]);

        $response = $this->actingAs($user)->get('/support-cockpit');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component('Foundry/SupportCockpit'));
    }

    public function test_threads_route_redirects_to_dashboard(): void
    {
        $user = User::factory()->create();

        $response = $this->actingAs($user)->get('/threads');

        $response->assertRedirect(route('dashboard'));
    }

    public static function projectRoutes(): array
    {
        return [
            'audit' => ['/audit', 'Foundry/AuditLog'],
            'targets' => ['/targets', 'Foundry/Targets'],
            'compare' => ['/compare', 'Foundry/HoleCompare'],
            'ingest-quality' => ['/imports/quality', 'Foundry/IngestQuality'],
            'analytics' => ['/analytics', 'Foundry/ProjectAnalytics'],
            'whats-changed' => ['/whats-changed', 'Foundry/WhatChangedFeed'],
            'saved-views' => ['/saved-views', 'Foundry/SavedMapViews'],
            'decisions' => ['/decisions', 'Foundry/Decisions'],
            'explorer' => ['/explorer', 'Foundry/Explorer'],
            'workspace' => ['/workspace', 'Foundry/Workspace'],
            'reasoning' => ['/reasoning', 'Foundry/Reasoning'],
            'hypothesis' => ['/hypothesis', 'Foundry/Reasoning'],
            'graph' => ['/graph', 'Foundry/SourceGraph'],
            'sources' => ['/sources', 'Foundry/Sources'],
            'corpus' => ['/corpus', 'Foundry/Corpus'],
            'reports' => ['/reports', 'Foundry/Report'],
            'investigations' => ['/investigations', 'Foundry/Investigations'],
            // §B/S/G build-out 2026-05-22 — Lakehouse inventory page.
            'lakehouse' => ['/lakehouse', 'Foundry/Lakehouse'],
        ];
    }

    #[DataProvider('projectRoutes')]
    public function test_project_route_renders_for_member(string $suffix, string $expectedComponent): void
    {
        $project = Project::query()->first();

        if (! $project) {
            $this->markTestSkipped('No projects in DB.');
        }

        $user = User::factory()->create();
        $project->users()->syncWithoutDetaching([$user->id => ['role' => 'viewer']]);

        $url = '/projects/'.$project->slug.$suffix;
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component($expectedComponent));
    }

    public function test_retrieval_inspector_renders_with_invalid_uuid(): void
    {
        $user = User::factory()->create();

        $response = $this->actingAs($user)->get('/retrieval/not-a-uuid');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page->component('Foundry/RetrievalInspector')->where('empty', true));
    }
}
