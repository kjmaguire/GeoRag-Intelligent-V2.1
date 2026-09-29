<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * silver.collars.geom (SRID 32613 for every collar on earth) is retired —
 * Kyle, 2026-09-29, §04e. geom_4326 is the only collar geometry.
 *
 * Pins the retirement migration's shape (loud, reversible) and that no
 * Laravel reader of silver.collars still names the dropped column. The
 * schema itself is asserted by database/tests/pgtap/08_silver_mvt_functions.sql
 * (hasnt_column / col_type_is).
 */
final class SilverCollarsGeomRetiredTest extends TestCase
{
    private const MIGRATION = '/database/migrations/2026_09_30_100000_drop_silver_collars_geom.php';

    private function root(): string
    {
        return dirname(__DIR__, 2);
    }

    private function migration(): string
    {
        $path = $this->root().self::MIGRATION;
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    /**
     * @return array{0: string, 1: string} [up body, down body]
     */
    private function upAndDown(): array
    {
        $sql = $this->migration();
        $upAt = strpos($sql, 'public function up(): void');
        $downAt = strpos($sql, 'public function down(): void');
        $this->assertNotFalse($upAt);
        $this->assertNotFalse($downAt);

        return [substr($sql, $upAt, $downAt - $upAt), substr($sql, $downAt)];
    }

    #[Test]
    public function up_drops_the_column_its_index_and_its_derive_trigger(): void
    {
        [$up] = $this->upAndDown();

        $this->assertStringContainsString('DROP TRIGGER IF EXISTS trg_collars_derive_geom_4326 ON silver.collars', $up);
        $this->assertStringContainsString('DROP FUNCTION IF EXISTS silver.collars_derive_geom_4326()', $up);
        $this->assertStringContainsString('DROP INDEX IF EXISTS silver.idx_collars_geom', $up);
        $this->assertStringContainsString('ALTER TABLE silver.collars DROP COLUMN IF EXISTS geom', $up);
    }

    #[Test]
    public function up_fails_loudly_rather_than_cascading_into_dependent_views(): void
    {
        [$up] = $this->upAndDown();

        $this->assertStringNotContainsStringIgnoringCase('CASCADE', $up);
    }

    #[Test]
    public function down_restores_the_32613_column_from_geom_4326(): void
    {
        [, $down] = $this->upAndDown();

        $this->assertStringContainsString('ADD COLUMN geom geometry(Point, 32613)', $down);
        $this->assertStringContainsString('SET geom = ST_Transform(geom_4326, 32613)', $down);
        $this->assertStringContainsString('CREATE INDEX IF NOT EXISTS idx_collars_geom ON silver.collars USING GIST (geom)', $down);
        $this->assertStringContainsString('CREATE TRIGGER trg_collars_derive_geom_4326', $down);
    }

    #[Test]
    public function no_laravel_collar_reader_names_the_retired_column(): void
    {
        $files = [
            '/app/Http/Controllers/Api/V1/CollarController.php',
            '/app/Http/Resources/CollarResource.php',
            '/app/Http/Controllers/Foundry/WorkspaceController.php',
        ];

        foreach ($files as $file) {
            $src = (string) file_get_contents($this->root().$file);
            $this->assertDoesNotMatchRegularExpression(
                '/ST_(?:X|Y|SRID|Transform)\(\s*(?:c\.)?geom\s*[,)]/',
                $src,
                "{$file} still reads silver.collars.geom",
            );
        }

        $controller = (string) file_get_contents($this->root().'/app/Http/Controllers/Api/V1/CollarController.php');
        $this->assertStringContainsString('ST_X(geom_4326) AS longitude, ST_Y(geom_4326) AS latitude', $controller);
    }
}
