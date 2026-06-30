<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\Http;

/**
 * GeoPackage exporter — proxies to FastAPI's GDAL-based export endpoint.
 *
 * Returns array{path: string, size: int}.
 */
class GeoPackageExporter
{
    public function export(string $projectId, array $filters = []): array
    {
        $fastApiUrl = rtrim(config('services.fastapi.internal_url'), '/');
        $serviceKey = config('services.fastapi.service_key');

        $response = Http::withHeaders([
            'X-Service-Key' => $serviceKey,
            'Accept' => 'application/geopackage+sqlite3',
        ])
            ->timeout(60)
            ->post("{$fastApiUrl}/internal/exports/geopackage", [
                'project_id' => $projectId,
                'format' => 'geopackage',
            ]);

        if (! $response->successful()) {
            throw new \RuntimeException(
                "FastAPI geopackage export failed: HTTP {$response->status()} — "
                .substr($response->body(), 0, 200),
            );
        }

        $tmpPath = sys_get_temp_dir().'/georag_collars_'.uniqid().'.gpkg';
        file_put_contents($tmpPath, $response->body());

        return [
            'path' => $tmpPath,
            'size' => filesize($tmpPath),
        ];
    }
}
