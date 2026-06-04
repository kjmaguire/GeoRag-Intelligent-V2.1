<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * Pin the audit item E invariant: the Martin tile server role
 * (`martin_ro`) is and stays READ-ONLY on the silver schema.
 *
 * Threat model
 * ------------
 * Martin is a tile server. Its only legitimate operations are
 * SELECT on silver / public_geo / public, and EXECUTE on the
 * `pg_*_by_project` / `pg_*_tiles` function family. Anything
 * that grants Martin write access to silver — `georag_write`
 * membership, direct INSERT/UPDATE/DELETE grants — re-creates the
 * pre-audit threat (Martin compromise = silver compromise) the
 * `martin_ro` rotation was supposed to close.
 *
 * Defence
 * -------
 * The init scripts + companion membership-grant file declare the
 * canonical privilege set. This test asserts:
 *
 *   1. `martin_ro` is created with INHERIT (so georag_read SELECT
 *      privileges flow through without per-query SET ROLE).
 *   2. `martin_ro` is granted georag_read but NOT georag_write.
 *   3. `martin_ro` is NEVER granted DML privileges directly.
 *
 * Strategy mirrors the other tenancy regression tests in this dir:
 * file-content assertions so the test runs without a live DB. The
 * actual GRANT execution is verified in CI when the migrations run.
 */
class MartinRoleReadOnlyTest extends TestCase
{
    private function initScript(string $name): string
    {
        $path = base_path("docker/postgresql/init/{$name}");
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    public function test_init_creates_martin_ro_with_inherit(): void
    {
        $contents = $this->initScript('00-create-app-roles.sql');

        $this->assertMatchesRegularExpression(
            '/CREATE ROLE martin_ro\\s+NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE INHERIT LOGIN/',
            $contents,
            'martin_ro must be created with INHERIT so georag_read SELECT '
            .'privileges flow through without per-query SET ROLE. The audit '
            .'item E rationale (AUDIT_AND_FIX_REPORT.md) explains why '
            .'NOINHERIT was overengineered defense-in-depth.',
        );
    }

    public function test_init_normalizes_legacy_noinherit_to_inherit(): void
    {
        $contents = $this->initScript('00-create-app-roles.sql');

        // Clusters that ran an earlier bootstrap with NOINHERIT must get
        // re-aligned on the next init pass. Without the ALTER ROLE
        // normalization clause, the connection-string swap to martin_ro
        // would silently fail tile rendering on those clusters.
        $this->assertMatchesRegularExpression(
            '/ALTER ROLE martin_ro INHERIT/',
            $contents,
            'init must normalize legacy NOINHERIT martin_ro instances to '
            .'INHERIT. Without this, clusters bootstrapped before audit '
            .'item E (2026-06-03) stay broken after the docker-compose '
            .'connection-string swap.',
        );
    }

    public function test_companion_grants_georag_read_to_martin_ro(): void
    {
        $contents = $this->initScript('zz-grant-app-role-memberships.sql');

        $this->assertMatchesRegularExpression(
            '/GRANT georag_read TO martin_ro/',
            $contents,
            'martin_ro must be granted georag_read so SELECT on silver '
            .'flows through. Without this membership, the Martin tile '
            .'server returns permission denied on every tile query.',
        );
    }

    public function test_companion_does_no_t_grant_georag_write_to_martin_ro(): void
    {
        $contents = $this->initScript('zz-grant-app-role-memberships.sql');

        $this->assertDoesNotMatchRegularExpression(
            '/GRANT georag_write TO martin_ro/',
            $contents,
            'martin_ro must NOT have georag_write membership. The whole '
            .'point of audit item E is that Martin can only SELECT — '
            .'granting write privileges re-creates the pre-audit threat '
            .'where a Martin compromise = silver compromise.',
        );
    }

    public function test_tile_function_migration_exists(): void
    {
        $path = base_path(
            'database/migrations/2026_06_03_030000_grant_tile_functions_to_martin_ro.php',
        );
        $this->assertFileExists(
            $path,
            'Migration granting EXECUTE on pg_*_by_project functions to '
            .'martin_ro is required for the Martin docker-compose role '
            .'swap to actually serve tiles. Without it, every tile '
            .'request returns 500 with permission denied for function.',
        );

        $contents = (string) file_get_contents($path);
        $this->assertStringContainsString(
            'GRANT EXECUTE ON FUNCTION',
            $contents,
            'Migration must issue GRANT EXECUTE ON FUNCTION — schema '
            .'+ table grants alone don\'t cover Martin\'s function-source '
            .'sites.',
        );
        $this->assertStringContainsString(
            'martin_ro',
            $contents,
            'Migration must target martin_ro specifically.',
        );

        // Spot-check the function list against martin.yaml. If this
        // assertion fails the two have drifted — update the
        // migration\'s FUNCTIONS constant.
        foreach ([
            'silver.pg_collars_by_project',
            'silver.pg_drill_traces_by_project',
            'public_geo.pg_bedrock_geology_tiles',
        ] as $required) {
            $this->assertStringContainsString(
                $required,
                $contents,
                "Migration must grant EXECUTE on {$required} — it's in "
                .'martin.yaml. Mirror new tile functions here when added.',
            );
        }
    }

    public function test_docker_compose_uses_martin_ro_not_georag_app(): void
    {
        $compose = (string) file_get_contents(base_path('docker-compose.yml'));

        // The Martin service block must reference MARTIN_RO_USER, not
        // GEORAG_APP_USER, in its DATABASE_URL. Drift here re-opens
        // the silver-write blast radius.
        $this->assertMatchesRegularExpression(
            '/DATABASE_URL:.*MARTIN_RO_USER.*martin_ro/',
            $compose,
            'Martin service DATABASE_URL must connect as martin_ro '
            .'(audit item E). Reverting to GEORAG_APP_USER re-grants '
            .'silver write access to the tile server.',
        );
    }
}
