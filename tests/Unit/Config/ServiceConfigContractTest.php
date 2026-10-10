<?php

declare(strict_types=1);

namespace Tests\Unit\Config;

use App\Jobs\GenerateExportJob;
use App\Jobs\StreamQueryFromFastApi;
use App\Models\User;
use App\Providers\HorizonServiceProvider;
use Illuminate\Support\Facades\Config;
use Illuminate\Support\Facades\Gate;
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
        //
        // This runs config/services.php itself with the variable set. It used
        // to call a private copy of the rule in this class ("mirrors the
        // normalisation") and assert on the copy, so the real file could
        // change, or stop reading HORIZON_ADMIN_EMAILS, and nothing noticed.
        $this->assertSame(
            ['ops@example.com', 'kyle@example.com'],
            $this->servicesConfigWithHorizonEnv('  Ops@Example.COM , kyle@example.com ,, ')['horizon']['admin_emails'],
        );
    }

    #[Test]
    public function an_unset_horizon_allowlist_denies_everyone(): void
    {
        // Fail closed is the deliberate behaviour: a deploy that forgets
        // HORIZON_ADMIN_EMAILS must not expose the queue dashboard. What
        // was NOT deliberate is that this was the behaviour even when the
        // variable WAS set, because nothing read it.
        foreach ([null, '', ' , ,'] as $raw) {
            $this->assertSame(
                [],
                $this->servicesConfigWithHorizonEnv($raw)['horizon']['admin_emails'],
                'an unset or blank HORIZON_ADMIN_EMAILS must yield an empty allowlist',
            );
        }

        // ...and an empty allowlist reaches the gate as "nobody", whoever asks.
        $this->useHorizonAllowlist([]);
        $this->assertFalse(Gate::forUser(User::factory()->make(['email' => 'ops@example.com']))->allows('viewHorizon'));
    }

    #[Test]
    public function the_view_horizon_gate_admits_only_the_allowlist(): void
    {
        // The Horizon dashboard shows queued job payloads, which for the llm
        // queue is the text of every question asked. This is the gate itself,
        // through Laravel's Gate, not a copy of its comparison.
        $this->useHorizonAllowlist(['ops@example.com']);

        $this->assertTrue(
            Gate::forUser(User::factory()->make(['email' => 'ops@example.com']))->allows('viewHorizon'),
            'a user on the allowlist must be admitted',
        );
        $this->assertTrue(
            Gate::forUser(User::factory()->make(['email' => 'Ops@Example.COM']))->allows('viewHorizon'),
            'the comparison is case-insensitive on the user side',
        );
        $this->assertFalse(
            Gate::forUser(User::factory()->make(['email' => 'someone.else@example.com']))->allows('viewHorizon'),
            'a user who is not on the allowlist must be refused',
        );
        $this->assertFalse(
            Gate::forUser(User::factory()->make(['email' => 'xops@example.com']))->allows('viewHorizon'),
            'a partial match must not admit',
        );
        $this->assertFalse(
            Gate::forUser(User::factory()->make(['email' => null]))->allows('viewHorizon'),
            'a user with no email must be refused',
        );
        $this->assertFalse(Gate::allows('viewHorizon'), 'a guest must be refused');
    }

    #[Test]
    public function the_horizon_dashboard_is_closed_outside_local_unless_the_gate_admits_the_user(): void
    {
        // The gate means nothing if the dashboard does not ask it. Through the
        // real route and middleware, in a non-local environment.
        $this->assertFalse(app()->environment('local'), 'premise: the local-environment escape hatch is closed here');
        $this->useHorizonAllowlist(['ops@example.com']);

        $this->getJson('/horizon/api/masters')->assertForbidden();

        $this->actingAs(User::factory()->make(['email' => 'someone.else@example.com']))
            ->getJson('/horizon/api/masters')
            ->assertForbidden();
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
    public function the_llm_queue_reservation_outlives_the_supervisor_and_the_job(): void
    {
        // CHAT-1. Laravel's RedisQueue stamps a reservation with
        // "now + retry_after" of the POPPING connection and re-queues it
        // once that passes. If retry_after <= the time a stream may run,
        // the job is re-queued mid-answer, the next llm worker sees
        // attempts > tries and runs failed(): a JOB_FAILED terminal while
        // the real answer is still streaming. It was 90 s against a 300 s
        // job for as long as supervisor-llm shared the `redis` connection.
        $supervisor = config('horizon.defaults.supervisor-llm');
        $this->assertIsArray($supervisor);

        $connectionName = $supervisor['connection'];
        $connection = config("queue.connections.{$connectionName}");
        $this->assertIsArray($connection, "supervisor-llm connection '{$connectionName}' is not a queue connection");
        $this->assertSame('redis', $connection['driver']);

        // Same Redis key space as the connection the job is dispatched
        // through, or the supervisor would pop a list nobody pushes to.
        $this->assertSame(
            config('queue.connections.redis.connection'),
            $connection['connection'],
            'redis-llm must reserve from the same Redis connection the job is pushed to',
        );

        $retryAfter = (int) $connection['retry_after'];
        $jobTimeout = StreamQueryFromFastApi::timeoutSeconds();

        foreach (['defaults', 'production', 'local'] as $env) {
            $timeout = $env === 'defaults'
                ? (int) $supervisor['timeout']
                : (int) (config("horizon.environments.{$env}.supervisor-llm.timeout") ?? $supervisor['timeout']);

            $this->assertGreaterThan($timeout, $retryAfter, "[{$env}] retry_after must exceed the supervisor-llm timeout");
            $this->assertGreaterThan($jobTimeout, $timeout, "[{$env}] supervisor-llm timeout must exceed the job timeout");
        }
    }

    #[Test]
    public function the_shared_redis_reservation_outlives_the_export_job(): void
    {
        // LAR-1's second half: GenerateExportJob runs on the shared `redis`
        // connection with a 300 s timeout. Any export slower than
        // retry_after was re-queued and failed while still running.
        $retryAfter = (int) config('queue.connections.redis.retry_after');
        $exportTimeout = (new \ReflectionClass(GenerateExportJob::class))
            ->getProperty('timeout')
            ->getDefaultValue();

        $this->assertGreaterThan($exportTimeout, $retryAfter);
    }

    #[Test]
    public function the_llm_supervisor_does_not_reserve_through_the_shared_redis_connection(): void
    {
        // The shared connection's retry_after is sized for 60 s default-queue
        // jobs. Pointing supervisor-llm back at it reintroduces CHAT-1 even
        // if the numbers above happen to be overridden by env.
        $this->assertNotSame('redis', config('horizon.defaults.supervisor-llm.connection'));
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
     * Run config/services.php with HORIZON_ADMIN_EMAILS set to $raw (null = unset) and return what it builds.
     *
     * @return array<string, mixed>
     */
    private function servicesConfigWithHorizonEnv(?string $raw): array
    {
        $name = 'HORIZON_ADMIN_EMAILS';
        $before = [
            '_ENV' => array_key_exists($name, $_ENV) ? $_ENV[$name] : null,
            '_SERVER' => array_key_exists($name, $_SERVER) ? $_SERVER[$name] : null,
        ];
        $had = ['_ENV' => array_key_exists($name, $_ENV), '_SERVER' => array_key_exists($name, $_SERVER)];

        try {
            // env() reads $_ENV and $_SERVER; set or clear both so a value from .env cannot shadow this one.
            foreach (['_ENV', '_SERVER'] as $bag) {
                if ($raw === null) {
                    unset($GLOBALS[$bag][$name]);
                } else {
                    $GLOBALS[$bag][$name] = $raw;
                }
            }

            /** @var array<string, mixed> $config */
            $config = require base_path('config/services.php');

            return $config;
        } finally {
            foreach (['_ENV', '_SERVER'] as $bag) {
                if ($had[$bag]) {
                    $GLOBALS[$bag][$name] = $before[$bag];
                } else {
                    unset($GLOBALS[$bag][$name]);
                }
            }
        }
    }

    /**
     * Point the viewHorizon gate at an allowlist. The provider reads
     * services.horizon.admin_emails once, when it boots, so changing the
     * config afterwards does nothing until its gate is defined again.
     *
     * @param list<string> $emails
     */
    private function useHorizonAllowlist(array $emails): void
    {
        Config::set('services.horizon.admin_emails', $emails);
        (new HorizonServiceProvider($this->app))->boot();
    }
}
