# Self-Hosting DEM + Satellite Base Tiles

**V1.5-12** — operator playbook for replacing the public-CDN base layers
(`tiles.mapterhorn.com` DEM + `tiles.maps.eox.at` Sentinel-2) with
self-hosted endpoints. Required for air-gapped deployments and the
"no external CDN" posture mining clients prefer.

## Why self-host

The dev stack defaults to free public tiles for two layers:

| Layer | Default CDN | Format |
|-------|-------------|--------|
| DEM (terrain + hillshade) | `https://tiles.mapterhorn.com/tilejson.json` | terrain-RGB raster |
| Satellite imagery | `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg` | JPEG raster |

Both are reliable and free, but:
- **Air-gapped clients** (mining sites with no public internet egress) can't reach them at all.
- **Privacy** — the CDN sees the bbox of every client query (which projects you're looking at).
- **Availability** — no SLA. A free CDN going down breaks the map UX.

Replacing them is not free. You need ~5-50 GB of tile storage (depends on
resolution + project bbox), one-time data acquisition + tile-encoding
work, and a small static or Martin-served HTTP endpoint.

## Architecture overview

The frontend reads two Vite env vars at build time:

```
VITE_DEM_TILES_URL=https://tiles.your-georag-domain.example.com/dem/tilejson.json
VITE_SATELLITE_TILES_URL=https://tiles.your-georag-domain.example.com/sat/{z}/{y}/{x}.jpg
```

`MapView.tsx` falls back to the public CDNs if either var is unset (per
`resources/js/Components/MapView.tsx` `DEFAULT_DEM_URL` /
`DEFAULT_SATELLITE_TILE_URL`). Set both to your hosted URLs in
`.env.production` and rebuild the SPA.

## Self-hosting the DEM (terrain-RGB)

### Step 1 — acquire DEM data

Pick a source for the project's geographic region. Free options:

| Source | Coverage | Resolution | License |
|--------|----------|------------|---------|
| SRTM 30 m | ±60° latitude, global | 30 m | Public domain (NASA) |
| AW3D30 | Global | 30 m | Free for non-commercial; permissive license for commercial with attribution |
| NRCan CDEM | Canada | 20 m (varies) | Open Government License — Canada |
| Copernicus DEM (GLO-30 / GLO-90) | Global, including poles | 30 m / 90 m | Free (ESA Copernicus) |

For a Saskatchewan project, NRCan CDEM is highest quality. For a Quebec /
BC project, Copernicus GLO-30 is the cleanest global option.

Download the GeoTIFF tiles for your project's bbox. Tools: `wget` for the
public S3 buckets, or `eio` (Earth Engine Import Output) for Copernicus.

### Step 2 — encode as terrain-RGB

MapLibre's `raster-dem` source expects pixels encoded as Mapbox terrain-RGB
(elevation = -10000 + ((R * 256² + G * 256 + B) * 0.1)). Tooling:

```bash
# Install rio-rgbify (rasterio plugin, MIT-licensed).
pip install rio-rgbify

# Convert SRTM/DEM GeoTIFF to MapLibre terrain-RGB.
rio rgbify \
    -b -10000 \
    -i 0.1 \
    --max-z 14 \
    --min-z 0 \
    --format webp \
    input_dem.tif \
    output_terrain_rgb.mbtiles
```

`webp` format halves bytes vs PNG with no quality loss for DEM. `--max-z 14`
matches the existing `MapView.DEM_SOURCE_CONFIG.maxzoom` — terrain detail
flatlines beyond that and tiles balloon.

### Step 3 — serve the tiles

Two patterns; pick one:

#### Option A — Martin raster_sources (preferred)

Martin 1.7.0+ supports MBTiles raster sources. Add to
`docker/martin/martin.yaml`:

```yaml
mbtiles:
    sources:
        dem: /tiles/output_terrain_rgb.mbtiles
        # If you also have a satellite mbtiles:
        sat: /tiles/satellite.mbtiles
```

Mount the .mbtiles files into the martin container:

```yaml
# docker-compose.yml — martin service
volumes:
  - ./docker/martin/martin.yaml:/config/martin.yaml:ro
  - ./tiles:/tiles:ro          # NEW
```

Martin will serve `https://martin.example.com/dem/tilejson.json` and
`/dem/{z}/{x}/{y}` automatically. The TileProxy controller from Module 8
Chunk 8.4 already gates the `/tiles/*` route by workspace; for global
DEM tiles add a separate unauthenticated proxy or expose Martin directly
behind nginx with an IP allowlist.

#### Option B — nginx static file server

For a simpler one-shot, untar pre-generated tile pyramid into nginx:

```bash
# Extract MBTiles to a flat directory (z/x/y.png).
mb-util output_terrain_rgb.mbtiles ./dem-tiles/

# nginx vhost:
server {
    server_name tiles.your-georag-domain.example.com;
    listen 443 ssl http2;

    location /dem/ {
        alias /var/www/dem-tiles/;
        types {
            image/webp webp;
            application/json tilejson;
        }
        # 1-year cache; tiles are immutable per (z,x,y).
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

You also need to author the `tilejson.json` file by hand (~10 lines —
copy the schema from `https://tiles.mapterhorn.com/tilejson.json` and
swap the URL field).

### Step 4 — point the SPA

Set in `.env.production`:

```
VITE_DEM_TILES_URL=https://tiles.your-georag-domain.example.com/dem/tilejson.json
```

Rebuild the SPA (Vite injects the value at compile time):

```bash
docker compose exec laravel-octane npm run build
```

Validate by loading the map and inspecting Network tab — DEM requests
should hit `tiles.your-georag-domain.example.com`, not `mapterhorn`.

## Self-hosting satellite imagery

Same pattern, different data source. EOX Sentinel-2 cloudless mosaic is
licensed CC-BY 4.0 — you can clip a project-bbox subset and host it.
Alternatives:

| Source | License | Notes |
|--------|---------|-------|
| Sentinel-2 L2A (raw) | Free (ESA Copernicus) | Cloud-mask + mosaic yourself; needs significant processing |
| Maxar SecureWatch | Commercial (per-km²) | Sub-meter quality; for client deliverables |
| Landsat 8/9 | Free (USGS) | 30 m; cloud-frequent |

Tile generation: `gdal2tiles.py` against a JPEG mosaic or `rio mbtiles`
against a Sentinel COG.

```bash
gdal2tiles.py -z 0-15 --processes=8 --webviewer=none satellite_mosaic.tif sat-tiles/
```

Serve via the same Martin / nginx pattern as DEM.

## Storage budget

Rough sizing per project:

| Item | Resolution | Bbox | Approx. size |
|------|------------|------|-------------|
| DEM terrain-RGB, z 0-14, webp | 30 m | 100 × 100 km project | 200-500 MB |
| Satellite Sentinel-2, z 0-15, jpeg | 10 m | 100 × 100 km project | 1-5 GB |

Multi-project deployments should consolidate under a single global tile
pyramid covering all client bboxes; otherwise per-project storage adds up.

## Operator checklist

1. [ ] Decide DEM source per project geography.
2. [ ] Download raw DEM GeoTIFFs.
3. [ ] Run `rio rgbify` to produce terrain-RGB MBTiles.
4. [ ] Decide DEM serving pattern (Martin vs nginx).
5. [ ] Point `VITE_DEM_TILES_URL` at the new endpoint.
6. [ ] Rebuild SPA (`npm run build`).
7. [ ] Repeat for satellite if required (skip for DEM-only minimal setup).
8. [ ] Test in browser — DEM + hillshade render at the project bbox.
9. [ ] Verify no `mapterhorn.com` / `eox.at` requests in Network tab.
10. [ ] Document the storage location + refresh cadence (DEM is static;
        satellite can be refreshed annually as new imagery becomes available).

## Memory cross-reference

This runbook closes the V1.5 backlog item `v1.5-12 DEM self-host` and
the deferred concern in `feedback_map_performance.md` ("next step is
self-host DEM if CDN latency persists").

After this lands the only remaining external CDN dependency for V1 is
the OpenFreeMap base style at `https://tiles.openfreemap.org/styles/positron`
(used as the vector basemap for non-satellite views) — that one's also
self-hostable but is out of scope for V1.5-12; track separately if a
client requests fully-air-gapped tooling.

## When NOT to self-host

- **Connected client deployments** where external CDN is acceptable.
- **Demos** — public CDNs are fine for sales conversations.
- **Limited project geography** (single small bbox) — self-host effort
  exceeds the latency / privacy gain.

The default-fallback behavior in `MapView.tsx` is intentional — V1.5-12
makes self-hosting a configuration choice, not a forced migration.
