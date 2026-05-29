<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\Http;

/**
 * Shapefile exporter — proxies to FastAPI's GDAL-based export endpoint.
 *
 * The FastAPI container has geopandas + GDAL installed. Laravel sends the
 * project_id, receives a ZIP containing .shp/.shx/.dbf/.prj files, and
 * saves it to the local temp directory for MinIO upload.
 *
 * Returns array{path: string, size: int}.
 */
class ShapefileExporter
{
    public function export(string $projectId, array $filters = []): array
    {
        $fastApiUrl = rtrim(config('services.fastapi.internal_url'), '/');
        $serviceKey = config('services.fastapi.service_key');

        $response = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Accept'        => 'application/zip',
            ])
            ->timeout(60)
            ->post("{$fastApiUrl}/internal/exports/shapefile", [
                'project_id' => $projectId,
                'format'     => 'shapefile',
            ]);

        if (! $response->successful()) {
            throw new \RuntimeException(
                "FastAPI shapefile export failed: HTTP {$response->status()} — "
                . substr($response->body(), 0, 200)
            );
        }

        $tmpPath = sys_get_temp_dir() . '/georag_collars_shapefile_' . uniqid() . '.zip';
        file_put_contents($tmpPath, $response->body());

        return [
            'path' => $tmpPath,
            'size' => filesize($tmpPath),
        ];
    }
}
