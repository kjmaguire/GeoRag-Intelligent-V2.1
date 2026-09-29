<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;
use Symfony\Component\Yaml\Yaml;

/**
 * SEC-1 / SEC-3: the on-prem (Helm) topology must keep Postgres RLS in play.
 *
 * Checked against the committed renders in kubernetes/manifests/, which
 * scripts/regenerate_k8s_manifests.sh produces from charts/georag/. Found
 * 2026-09-29: Laravel and Martin connected as `georag` — the image's
 * POSTGRES_USER, i.e. a superuser, which bypasses RLS even under FORCE —
 * the Hatchet worker fell back to it, the ingress routed /tiles straight to
 * Martin (shadowing Laravel's authenticating tile proxy) and /internal
 * straight to FastAPI, and Martin ran with no config, so it auto-published
 * every geometry table unfiltered.
 */
final class HelmTenantIsolationManifestTest extends TestCase
{
    /** Components that legitimately hold the owner/superuser role. */
    private const OWNER_COMPONENTS = ['postgresql', 'pgbouncer'];

    /**
     * @return array<string, array{0: string}>
     */
    public static function renders(): array
    {
        $cases = [];
        foreach (glob(dirname(__DIR__, 2).'/kubernetes/manifests/*.yaml') ?: [] as $path) {
            $cases[basename($path)] = [$path];
        }

        return $cases;
    }

    public function test_the_chart_martin_config_is_the_repo_martin_config(): void
    {
        $root = dirname(__DIR__, 2);

        $this->assertFileEquals(
            $root.'/docker/martin/martin.yaml',
            $root.'/charts/georag/files/martin.yaml',
            'charts/georag/files/martin.yaml drifted from docker/martin/martin.yaml — copy it across.',
        );
        $this->assertMatchesRegularExpression(
            '/^\s*auto_publish:\s*false\s*$/m',
            (string) file_get_contents($root.'/charts/georag/files/martin.yaml'),
        );
    }

    #[DataProvider('renders')]
    public function test_no_runtime_workload_connects_as_the_superuser(string $path): void
    {
        foreach ($this->workloads($path) as $component => $containers) {
            if (in_array($component, self::OWNER_COMPONENTS, true)) {
                continue;
            }
            foreach ($containers as $name => $env) {
                foreach (['DB_USERNAME', 'POSTGRES_USER'] as $key) {
                    if (array_key_exists($key, $env)) {
                        $this->assertNotSame('georag', $env[$key], "{$component}/{$name} {$key} is the superuser in ".basename($path));
                    }
                }
                if (isset($env['DATABASE_URL'])) {
                    $this->assertStringNotContainsString('://georag:', $env['DATABASE_URL'], "{$component}/{$name} DATABASE_URL is the superuser");
                }
            }
        }
    }

    #[DataProvider('renders')]
    public function test_every_database_client_names_a_non_superuser_role(string $path): void
    {
        $workloads = $this->workloads($path);

        $expect = [
            'laravel-octane' => ['DB_USERNAME', 'georag_app'],
            'laravel-horizon' => ['DB_USERNAME', 'georag_app'],
            'laravel-reverb' => ['DB_USERNAME', 'georag_app'],
            'fastapi' => ['POSTGRES_USER', 'georag_app'],
            'hatchet-worker' => ['POSTGRES_USER', 'georag_app'],
        ];

        foreach ($expect as $component => [$key, $role]) {
            if (! isset($workloads[$component])) {
                continue;
            }
            foreach ($workloads[$component] as $name => $env) {
                $this->assertSame($role, $env[$key] ?? null, "{$component}/{$name} must set {$key}={$role} explicitly in ".basename($path));
            }
        }

        if (isset($workloads['martin'])) {
            foreach ($workloads['martin'] as $env) {
                $this->assertStringStartsWith('postgresql://martin_readonly:', $env['DATABASE_URL'] ?? '');
            }
        }
    }

    #[DataProvider('renders')]
    public function test_laravel_octane_is_not_behind_the_transaction_pooler(string $path): void
    {
        foreach ($this->workloads($path)['laravel-octane'] ?? [] as $env) {
            $this->assertStringEndsNotWith('-pgbouncer', $env['DB_HOST'] ?? '', 'SEC-2: session-scoped RLS binding behind PgBouncer');
            $this->assertNotSame('6432', $env['DB_PORT'] ?? null);
            $this->assertSame('false', $env['DB_POOLED'] ?? null);
        }
    }

    #[DataProvider('renders')]
    public function test_martin_runs_the_repo_config(string $path): void
    {
        foreach ($this->documents($path) as $doc) {
            if (($doc['kind'] ?? null) !== 'Deployment' || ! str_ends_with((string) ($doc['metadata']['name'] ?? ''), '-martin')) {
                continue;
            }
            $container = $doc['spec']['template']['spec']['containers'][0];
            $this->assertSame(['--config', '/config/martin.yaml'], $container['args'] ?? null, 'Martin without --config auto-publishes every table');

            return;
        }

        $this->markTestSkipped('No Martin Deployment in '.basename($path));
    }

    #[DataProvider('renders')]
    public function test_the_ingress_routes_only_to_laravel(string $path): void
    {
        foreach ($this->documents($path) as $doc) {
            if (($doc['kind'] ?? null) !== 'Ingress') {
                continue;
            }
            foreach ($doc['spec']['rules'] ?? [] as $rule) {
                foreach ($rule['http']['paths'] ?? [] as $p) {
                    $service = (string) $p['backend']['service']['name'];
                    $this->assertMatchesRegularExpression(
                        '/-laravel-(octane|reverb)$/',
                        $service,
                        "Ingress path {$p['path']} bypasses Laravel to {$service} in ".basename($path),
                    );
                }
            }
        }
    }

    /**
     * @return list<array<string, mixed>>
     */
    private function documents(string $path): array
    {
        $docs = [];
        foreach (preg_split('/^---\s*$/m', (string) file_get_contents($path)) ?: [] as $chunk) {
            if (trim($chunk) === '') {
                continue;
            }
            $parsed = Yaml::parse($chunk);
            if (is_array($parsed)) {
                $docs[] = $parsed;
            }
        }

        return $docs;
    }

    /**
     * component => container name => [env name => literal value].
     *
     * @return array<string, array<string, array<string, string>>>
     */
    private function workloads(string $path): array
    {
        $out = [];
        foreach ($this->documents($path) as $doc) {
            if (! in_array($doc['kind'] ?? null, ['Deployment', 'StatefulSet'], true)) {
                continue;
            }
            $template = $doc['spec']['template'] ?? [];
            $component = (string) ($template['metadata']['labels']['app.kubernetes.io/component'] ?? $doc['metadata']['name']);
            foreach ($template['spec']['containers'] ?? [] as $container) {
                $env = [];
                foreach ($container['env'] ?? [] as $var) {
                    if (array_key_exists('value', $var)) {
                        $env[(string) $var['name']] = (string) $var['value'];
                    }
                }
                $out[$component][(string) $container['name']] = $env;
            }
        }

        return $out;
    }
}
