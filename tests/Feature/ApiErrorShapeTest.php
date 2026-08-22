<?php

declare(strict_types=1);

namespace Tests\Feature;

use Illuminate\Support\Facades\Route;
use Tests\TestCase;

/**
 * An API client asked for JSON, so an error is JSON too.
 *
 * The exception handler routes 403/404/419/429/500/503 through the branded
 * Inertia Error page so a browser always has a path back into the app. It had
 * no guard on who was asking. fetch() and API clients do not send the
 * X-Inertia header, so Inertia fell back to rendering the full HTML root view
 * and the documented /api/v1 JSON API answered errors with 2.6 KB of
 * text/html. `response.json()` then threw, and the client reported a parse
 * failure rather than the reason.
 *
 * It hid errors from the SPA too: DataImportWizard does
 * `await res.json().catch(() => ({}))`, so the HTML was swallowed and the
 * user saw a bare "HTTP 500" instead of "File upload failed."
 *
 * These run with app.debug off, because the handler is a no-op when it is on.
 */
final class ApiErrorShapeTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        config(['app.debug' => false]);
    }

    public function test_an_unknown_api_route_answers_json(): void
    {
        $response = $this->getJson('/api/v1/does-not-exist');

        $response->assertNotFound();
        $this->assertStringContainsString(
            'application/json',
            (string) $response->headers->get('Content-Type'),
        );
    }

    public function test_an_api_route_under_the_prefix_answers_json_even_without_an_accept_header(): void
    {
        // The `is('api/*')` half of the guard: a client that forgot its
        // Accept header is still an API client.
        Route::get('/api/v1/_test/boom', fn () => abort(403));

        $response = $this->get('/api/v1/_test/boom');

        $response->assertForbidden();
        // `data-page` is the attribute Inertia stringifies its page object
        // into on the root element. Its presence is what "this is the SPA
        // shell, not an API response" actually means — the content type
        // alone cannot tell the branded page apart from Laravel's own plain
        // HTML error, and only the former is wrong here.
        $this->assertStringNotContainsString('data-page', $response->getContent() ?: '');
    }

    public function test_a_browser_still_gets_the_branded_error_page(): void
    {
        // The behaviour the handler exists for: a stale citation deep link
        // must land somewhere with a way back into the app, not on Laravel's
        // bare framework error text.
        $response = $this->get('/definitely-not-a-page');

        $response->assertNotFound();
        $this->assertStringContainsString('data-page', $response->getContent() ?: '');
    }

    public function test_debug_mode_still_gets_the_full_trace(): void
    {
        config(['app.debug' => true]);

        $response = $this->get('/definitely-not-a-page');

        $response->assertNotFound();
    }
}
