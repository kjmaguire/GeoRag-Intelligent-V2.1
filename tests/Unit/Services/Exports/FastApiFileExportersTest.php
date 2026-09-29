<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Exports;

use App\Services\Exports\GeoPackageExporter;
use App\Services\Exports\ShapefileExporter;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * The Shapefile and GeoPackage exporters proxy to FastAPI and save the
 * response body as the export file. They must only ever save a file: an
 * empty project used to come back as `{"error": ...}` with HTTP 200 and was
 * written verbatim to a `.zip` / `.gpkg` the user then downloaded as a
 * corrupt file. No DB involved.
 */
class FastApiFileExportersTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.internal_url' => 'http://fastapi.test',
            'services.fastapi.service_key' => 'test-key',
        ]);
    }

    public function test_shapefile_export_throws_on_404_for_an_empty_project(): void
    {
        Http::fake([
            'fastapi.test/internal/exports/shapefile' => Http::response(
                ['detail' => 'No collar data found for this project'],
                404,
            ),
        ]);

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessage('HTTP 404');

        (new ShapefileExporter)->export('11111111-1111-1111-1111-111111111111');
    }

    public function test_shapefile_export_refuses_a_2xx_json_body(): void
    {
        Http::fake([
            'fastapi.test/internal/exports/shapefile' => Http::response(
                ['error' => 'No collar data found for this project'],
                200,
            ),
        ]);

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessage('JSON body instead of a file');

        (new ShapefileExporter)->export('11111111-1111-1111-1111-111111111111');
    }

    public function test_geopackage_export_refuses_a_2xx_json_body(): void
    {
        Http::fake([
            'fastapi.test/internal/exports/geopackage' => Http::response(
                ['error' => 'No collar data found for this project'],
                200,
            ),
        ]);

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessage('JSON body instead of a file');

        (new GeoPackageExporter)->export('11111111-1111-1111-1111-111111111111');
    }

    public function test_geopackage_export_saves_a_binary_body(): void
    {
        Http::fake([
            'fastapi.test/internal/exports/geopackage' => Http::response(
                'SQLite format 3',
                200,
                ['Content-Type' => 'application/geopackage+sqlite3'],
            ),
        ]);

        $result = (new GeoPackageExporter)->export('11111111-1111-1111-1111-111111111111');

        try {
            $this->assertFileExists($result['path']);
            $this->assertSame('SQLite format 3', file_get_contents($result['path']));
            $this->assertSame(15, $result['size']);
        } finally {
            @unlink($result['path']);
        }
    }
}
