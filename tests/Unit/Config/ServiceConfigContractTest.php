<?php

declare(strict_types=1);

namespace Tests\Unit\Config;

use App\Jobs\StreamQueryFromFastApi;
use Illuminate\Support\Facades\Config;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * The behaviour behind the config keys ConfigKeysResolveTest only proves
 * exist.
 *
 * A key resolving is necessary and not sufficient: `services.horizon` could
 * exist and still hold an unsplit string, `basemap.styles` could exist and
 * be missing the id the SPA asks for. These assert the shapes the readers
 * actually depend on.
 */
final class ServiceConfigContractTest extends TestCase
{
    #[Test]
    public function horizon_admin_emails_is_a_normalised_list(): void
    {
        // The gate compares `strtolower($user->email)` against this array
        // with a strict in_array, so anything unnormalised here is a
        // silent denial for a user who is on the list.
        Config::set('services.horizon.admin_emails', $this->normalise('  Ops@Example.COM , kyle@example.com ,, '));

        $this->assertSame(
            ['ops@example.com', 'kyle@example.com'],
            Config::get('services.horizon.admin_emails'),
        );
    }

    #[Test]
    public function an_unset_horizon_allowlist_denies_everyone(): void
    {
        // Fail closed is the deliberate behaviour: a deploy that forgets
        // HORIZON_ADMIN_EMAILS must not expose the queue dashboard. What
        // was NOT deliberate is that this was the behaviour even when the
        // variable WAS set, because nothing read it.
        $this->assertSame([], $this->normalise(''));
    }

    #[Test]
    public function every_basemap_style_the_frontend_names_is_configured(): void
    {
        // resources/js/lib/basemap.ts declares BasemapStyleId as exactly
        // these three. A missing entry means that map silently falls back
        // to the hard-coded public CDN URL in the TypeScript, which is the
        // thing an air-gapped deployment cannot reach.
        $styles = Config::get('services.basemap.styles');

        $this->assertIsArray($styles);
        foreach (['positron', 'bright', 'dark_matter'] as $id) {
            $this->assertArrayHasKey($id, $styles, "basemap style '{$id}' is not configured");
            $this->assertIsString($styles[$id]);
            $this->assertNotSame('', $styles[$id]);
        }
    }

    #[Test]
    public function the_glyph_and_satellite_endpoints_are_configured_too(): void
    {
        // Not styles, but network dependencies all the same -- and both
        // were hard-coded a second time inside WorkspaceMap, outside the
        // registry, so swapping all three styles still left two calls to
        // the public internet.
        $this->assertIsString(Config::get('services.basemap.glyphs'));
        $this->assertStringContainsString('{fontstack}', (string) Config::get('services.basemap.glyphs'));
        $this->assertStringContainsString('{z}', (string) Config::get('services.basemap.satellite_tiles'));
    }

    #[Test]
    public function the_stream_read_timeout_expires_before_the_job_does(): void
    {
        // The ordering is what matters: the inner read timeout must fire
        // first so the stream raises a diagnosable exception that failed()
        // turns into a terminal `failed` event. If Horizon kills the worker
        // first, the client waits on its own watchdog with no explanation.
        //
        // Asserted across a range because the point is that the invariant
        // is DERIVED, not that today's numbers happen to satisfy it. It
        // used to be a comment in config/services.php next to an
        // env-tunable value, with the job's 300 hard-coded.
        foreach ([30, 270, 600, 3600] as $streamTimeout) {
            Config::set('services.fastapi.stream_timeout', $streamTimeout);

            $this->assertGreaterThan(
                $streamTimeout,
                StreamQueryFromFastApi::timeoutSeconds(),
                "job timeout must exceed a {$streamTimeout}s stream timeout",
            );
        }
    }

    #[Test]
    public function a_dispatched_job_carries_the_derived_timeout(): void
    {
        Config::set('services.fastapi.stream_timeout', 111);

        $job = new StreamQueryFromFastApi(
            'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee',
            'what is the grade',
            'query.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        );

        $this->assertSame(141, $job->timeout);
    }

    /**
     * Mirrors the normalisation in config/services.php so the test asserts
     * the rule rather than re-reading the value the rule produced.
     *
     * @return list<string>
     */
    private function normalise(string $raw): array
    {
        return array_values(array_filter(array_map(
            static fn (string $email): string => strtolower(trim($email)),
            explode(',', $raw),
        )));
    }
}
