<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * GIS-21 (audit 2026-09-29): the collar tile source reads geom_4326.
 *
 * silver.collars.geom is pinned to SRID 32613 for every collar on earth;
 * geom_4326 is the position transformed straight from each collar's source
 * CRS. The LATEST migration that (re)defines silver.pg_collars_by_project
 * must build its tiles from geom_4326, so that `geom` can be retired
 * without a blank collar layer, and must keep the workspace scoping.
 */
final class CollarTileSourceReadsGeom4326Test extends TestCase
{
    private function latestDefinition(): string
    {
        $files = glob(dirname(__DIR__, 2).'/database/migrations/*.php') ?: [];
        sort($files);
        $latest = null;
        foreach ($files as $file) {
            $sql = (string) file_get_contents($file);
            if (str_contains($sql, 'CREATE OR REPLACE FUNCTION silver.pg_collars_by_project(')) {
                $latest = $sql;
            }
        }
        $this->assertNotNull($latest, 'no migration defines silver.pg_collars_by_project');

        return (string) $latest;
    }

    #[Test]
    public function tiles_are_built_from_geom_4326(): void
    {
        $sql = $this->latestDefinition();

        $this->assertStringContainsString('ST_Transform(c.geom_4326, 3857)', $sql);
        $this->assertStringContainsString('c.geom_4326 && tile_4326', $sql);
        $this->assertStringNotContainsString('ST_Transform(c.geom, 3857)', $sql);
        $this->assertStringNotContainsString('ST_Transform(c.geom, 4326)', $sql);
    }

    #[Test]
    public function workspace_scoping_is_kept(): void
    {
        $sql = $this->latestDefinition();

        $this->assertStringContainsString('workspace_id is required in query_params', $sql);
        $this->assertStringContainsString('c.workspace_id = v_wsid', $sql);
        $this->assertStringContainsString("set_config('app.workspace_id'", $sql);
    }
}
