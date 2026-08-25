import { Head, Link } from '@inertiajs/react';
import { useEffect, useMemo, useRef, useState } from 'react';
import AppLayout from '@/Layouts/AppLayout';
import { Card, EmptyState, PageHeader, Pill, Stat } from '@/Components/Foundry/primitives';
import { useBasemapStyleUrl } from '@/lib/basemap';

/**
 * RasterLayers — the project's raster catalogue.
 *
 * This page indexes rasters. It does NOT display them, and every part of it
 * is written so a reader cannot mistake one for the other: the map draws the
 * FOOTPRINT polygon a raster covers, never its pixels. The pixels are not
 * stored anywhere a browser can reach — no COG, no tile pyramid, no PNG
 * derivative — and serving them needs a tile path this deployment does not
 * have. Saying "indexed, not yet viewable" out loud is the whole point;
 * a rectangle on a basemap with no caption reads as a broken image layer.
 *
 * The two things a geologist actually gets from it today:
 *
 *   1. Proof the upload survived. For a measurement raster (a DEM, a
 *      magnetics grid) `tiff_normalize` stops before OCR on purpose, so
 *      there is no document, no passages and no chat answer — this row is
 *      the only evidence the file is in the system at all.
 *   2. A worklist. A raster with no CRS is a picture, not a map, and has to
 *      be georeferenced before it is worth anything.
 */

interface BandStat {
    band_index?: number;
    dtype?: string;
    min?: number | null;
    max?: number | null;
    mean?: number | null;
    nodata?: number | null;
    description?: string | null;
}

/** `{code, message, context}` as emitted by raster_parser.py. */
interface RasterWarning {
    code?: string;
    message?: string;
    context?: Record<string, unknown>;
}

/**
 * A GeoJSON Polygon as returned by ST_AsGeoJSON(bbox).
 *
 * Deliberately `any`, same escape hatch WorkspaceMap takes and for the same
 * reason: maplibre's GeoJSON types demand a literal `type: 'Polygon'` union
 * member, and a value decoded from a database column cannot prove that to
 * the compiler. The page never reads a field off it — it hands it to a
 * geojson source and nothing else.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type BboxGeometry = any;

interface RasterLayerRow {
    raster_id: string;
    layer_name: string;
    source_filename: string | null;
    source_file: string;
    source_file_sha256: string;
    format: string;
    driver: string | null;
    width: number;
    height: number;
    band_count: number;
    crs: string | null;
    crs_confidence: number | null;
    pixel_size_x: number | null;
    pixel_size_y: number | null;
    compression: string | null;
    is_cog: boolean;
    has_alpha: boolean;
    bounds_native: unknown[];
    band_stats: BandStat[];
    tags: Record<string, unknown> | null;
    warnings: RasterWarning[];
    warning_count: number;
    /** Footprint in EPSG:4326. Null when the parser could not reproject. */
    bbox: BboxGeometry | null;
    /** [west, south, east, north] — what the map fits to. */
    bounds: [number, number, number, number] | null;
    extent_km2: number | null;
    georeferenced: boolean;
    /** True when tiff_normalize skipped OCR: this file has no document. */
    ocr_skipped: boolean;
    created_at: string | null;
}

interface RasterSummary {
    total: number;
    georeferenced: number;
    missing_crs: number;
    missing_footprint: number;
    cloud_optimized: number;
    ocr_skipped: number;
    with_warnings: number;
    list_limit: number;
    truncated: boolean;
}

interface UngeoreferencedTiff {
    report_id: string;
    title: string;
    source_filename: string | null;
    created_at: string | null;
}

interface RasterLayersProps {
    project: { project_id: string; project_name: string; slug: string };
    rasters: RasterLayerRow[];
    summary: RasterSummary;
    ungeoreferenced: UngeoreferencedTiff[];
}

/** `4096 x 4096` → `16.8 MP`, because pixel counts are unreadable raw. */
function megapixels(width: number, height: number): string {
    const mp = (width * height) / 1_000_000;
    if (mp < 1) return `${(width * height).toLocaleString()} px`;
    return `${mp.toFixed(1)} MP`;
}

/**
 * Ground sample distance in the raster's OWN CRS units.
 *
 * Deliberately unit-less. pixel_size_x comes straight off the geotransform,
 * so it is metres for a UTM grid and DEGREES for anything stored in 4326 —
 * appending "m" would turn 0.0002° into a claim of sub-millimetre resolution.
 */
function pixelSize(x: number | null, y: number | null): string {
    if (x === null && y === null) return '—';
    const fmt = (v: number | null): string => {
        if (v === null) return '?';
        const a = Math.abs(v);
        return a >= 1 ? a.toFixed(2) : a.toPrecision(3);
    };
    return `${fmt(x)} x ${fmt(y)}`;
}

function extentLabel(km2: number | null): string {
    if (km2 === null) return '—';
    if (km2 < 1) return `${(km2 * 1_000_000).toFixed(0)} m²`;
    return `${km2 < 100 ? km2.toFixed(1) : Math.round(km2).toLocaleString()} km²`;
}

/**
 * Confidence is a 0-1 heuristic from raster_parser._score_crs_confidence,
 * not a percentage the parser measured. Rendered as a word plus the number
 * so a reader does not read "0.4" as "40% of the pixels are right".
 */
function confidenceLabel(c: number | null): { text: string; tone: 'accent' | 'warn' | 'neutral' } {
    if (c === null) return { text: 'unscored', tone: 'neutral' };
    if (c >= 0.8) return { text: `high · ${c.toFixed(2)}`, tone: 'accent' };
    if (c >= 0.5) return { text: `moderate · ${c.toFixed(2)}`, tone: 'neutral' };
    return { text: `low · ${c.toFixed(2)}`, tone: 'warn' };
}

/** A raster the map can actually place: it has both a footprint and bounds. */
type PlacedRaster = RasterLayerRow & {
    bbox: BboxGeometry;
    bounds: [number, number, number, number];
};

function isPlaced(r: RasterLayerRow): r is PlacedRaster {
    return r.bbox !== null && r.bounds !== null;
}

/**
 * Footprint map.
 *
 * One outlined polygon per raster with a bbox, over a plain basemap. The
 * fill is deliberately faint: a solid rectangle reads as an image tile, and
 * the fastest way to make this page lie would be to draw something that
 * looks like the map sheet itself.
 *
 * maplibre-gl is imported dynamically, matching WorkspaceMap — it is a large
 * bundle and a project with no georeferenced rasters never needs it.
 */
function FootprintMap({
    rasters,
    selectedId,
    onSelect,
}: {
    rasters: RasterLayerRow[];
    selectedId: string | null;
    onSelect: (id: string) => void;
}) {
    const containerRef = useRef<HTMLDivElement | null>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const mapRef = useRef<any>(null);
    // Read by the 'load' handler below. The handler is registered once, so
    // reading `selectedId` from its closure would paint the highlight for
    // whatever was selected when the map was BUILT — wrong every time the
    // raster list changes while a row is open.
    const selectedRef = useRef<string | null>(selectedId);
    selectedRef.current = selectedId;
    const styleUrl = useBasemapStyleUrl('positron');

    // A type guard, not a bare filter: `bounds` is read as a 4-tuple below
    // and TS cannot narrow `T[] | null` through `.filter()` on its own.
    const placed = useMemo(() => rasters.filter(isPlaced), [rasters]);

    const featureCollection = useMemo(
        () => ({
            type: 'FeatureCollection' as const,
            features: placed.map((r) => ({
                type: 'Feature' as const,
                geometry: r.bbox,
                properties: {
                    raster_id: r.raster_id,
                    layer_name: r.layer_name,
                    georeferenced: r.georeferenced,
                },
            })),
        }),
        [placed],
    );

    const overall = useMemo<[number, number, number, number] | null>(() => {
        if (placed.length === 0) return null;
        return placed.reduce<[number, number, number, number]>(
            (acc, r) => {
                const b = r.bounds;
                return [
                    Math.min(acc[0], b[0]),
                    Math.min(acc[1], b[1]),
                    Math.max(acc[2], b[2]),
                    Math.max(acc[3], b[3]),
                ];
            },
            [180, 90, -180, -90],
        );
    }, [placed]);

    useEffect(() => {
        if (!containerRef.current || overall === null) return;
        let cancelled = false;

        import('maplibre-gl').then((ml) => {
            if (cancelled || !containerRef.current) return;
            const maplibregl = ml.default ?? ml;

            if (mapRef.current?.remove) {
                mapRef.current.remove();
            }

            // A single-raster footprint is a degenerate bbox at high zoom;
            // padding it stops fitBounds from landing at maxZoom on a corner.
            const pad = 0.02;
            const map = new maplibregl.Map({
                container: containerRef.current,
                style: styleUrl,
                bounds: [
                    overall[0] - pad,
                    overall[1] - pad,
                    overall[2] + pad,
                    overall[3] + pad,
                ] as [number, number, number, number],
                fitBoundsOptions: { padding: 40, maxZoom: 12 },
                attributionControl: false,
            });
            mapRef.current = map;

            map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
            map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');

            map.on('load', () => {
                if (cancelled) return;
                map.addSource('raster-footprints', { type: 'geojson', data: featureCollection });
                map.addLayer({
                    id: 'raster-footprints-fill',
                    type: 'fill',
                    source: 'raster-footprints',
                    paint: { 'fill-color': '#38bdf8', 'fill-opacity': 0.08 },
                });
                map.addLayer({
                    id: 'raster-footprints-line',
                    type: 'line',
                    source: 'raster-footprints',
                    paint: { 'line-color': '#38bdf8', 'line-width': 1.5 },
                });
                map.addLayer({
                    id: 'raster-footprints-selected',
                    type: 'line',
                    source: 'raster-footprints',
                    filter: ['==', ['get', 'raster_id'], selectedRef.current ?? '__none__'],
                    paint: { 'line-color': '#f59e0b', 'line-width': 3 },
                });

                map.on('click', 'raster-footprints-fill', (e: { features?: Array<{ properties?: Record<string, unknown> }> }) => {
                    const id = e.features?.[0]?.properties?.raster_id;
                    if (typeof id === 'string') onSelect(id);
                });
                map.on('mouseenter', 'raster-footprints-fill', () => {
                    map.getCanvas().style.cursor = 'pointer';
                });
                map.on('mouseleave', 'raster-footprints-fill', () => {
                    map.getCanvas().style.cursor = '';
                });
            });
        });

        return () => {
            cancelled = true;
            if (mapRef.current?.remove) {
                mapRef.current.remove();
                mapRef.current = null;
            }
        };
        // featureCollection / overall are memoised on `rasters`; selectedId is
        // applied by the effect below instead so picking a row never rebuilds
        // the map and throws away the user's pan/zoom.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [featureCollection, overall, styleUrl]);

    useEffect(() => {
        const map = mapRef.current;
        if (!map || typeof map.getLayer !== 'function') return;
        if (!map.getLayer('raster-footprints-selected')) return;
        map.setFilter('raster-footprints-selected', ['==', ['get', 'raster_id'], selectedId ?? '__none__']);
    }, [selectedId]);

    if (overall === null) {
        return (
            <div
                className="flex items-center justify-center text-xs px-6 py-10 text-center"
                style={{ color: 'var(--fg-3)' }}
            >
                No raster in this project has a footprint in WGS84, so there is nothing to
                place on a map. Either the file carried no CRS, or its bounds could not be
                reprojected — the warnings on each row below say which.
            </div>
        );
    }

    return <div ref={containerRef} style={{ height: 320, width: '100%' }} />;
}

/** The expanded facts for one raster. */
function RasterDetail({ raster }: { raster: RasterLayerRow }) {
    const conf = confidenceLabel(raster.crs_confidence);

    return (
        <div
            className="px-4 py-3 border-t text-[11px]"
            style={{ borderColor: 'var(--line-1)', background: 'var(--bg-2)' }}
        >
            <div className="grid gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
                <Fact label="Source file" value={raster.source_file} mono />
                <Fact label="SHA-256" value={raster.source_file_sha256.slice(0, 16) + '…'} mono />
                <Fact label="Format / driver" value={[raster.format, raster.driver].filter(Boolean).join(' · ') || '—'} />
                <Fact label="Pixel size (CRS units)" value={pixelSize(raster.pixel_size_x, raster.pixel_size_y)} mono />
                <Fact label="Ground footprint" value={extentLabel(raster.extent_km2)} mono />
                <Fact label="Compression" value={raster.compression ?? 'none recorded'} />
                <Fact label="Alpha band" value={raster.has_alpha ? 'yes' : 'no'} />
                <Fact label="Cloud-optimized (COG)" value={raster.is_cog ? 'yes' : 'no'} />
                <Fact label="CRS confidence" value={conf.text} />
            </div>

            {raster.bounds && (
                <div className="mt-3 font-mono text-[10px]" style={{ color: 'var(--fg-3)' }}>
                    BBOX 4326 · W {raster.bounds[0].toFixed(5)} · S {raster.bounds[1].toFixed(5)} · E{' '}
                    {raster.bounds[2].toFixed(5)} · N {raster.bounds[3].toFixed(5)}
                </div>
            )}

            {raster.band_stats.length > 0 && (
                <div className="mt-3">
                    <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                        Bands
                    </div>
                    <div className="overflow-x-auto">
                        <table className="min-w-[420px] text-[10px] font-mono tabular-nums">
                            <thead>
                                <tr style={{ color: 'var(--fg-3)' }}>
                                    <th className="text-left pr-4 pb-1">#</th>
                                    <th className="text-left pr-4 pb-1">dtype</th>
                                    <th className="text-right pr-4 pb-1">min</th>
                                    <th className="text-right pr-4 pb-1">max</th>
                                    <th className="text-right pr-4 pb-1">mean</th>
                                    <th className="text-left pb-1">description</th>
                                </tr>
                            </thead>
                            <tbody style={{ color: 'var(--fg-2)' }}>
                                {raster.band_stats.map((b, i) => (
                                    <tr key={b.band_index ?? i}>
                                        <td className="pr-4">{b.band_index ?? i + 1}</td>
                                        <td className="pr-4">{b.dtype ?? '—'}</td>
                                        <td className="pr-4 text-right">{b.min ?? '—'}</td>
                                        <td className="pr-4 text-right">{b.max ?? '—'}</td>
                                        <td className="pr-4 text-right">
                                            {typeof b.mean === 'number' ? b.mean.toFixed(3) : '—'}
                                        </td>
                                        <td>{b.description ?? ''}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {raster.warnings.length > 0 && (
                <div className="mt-3">
                    <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--warn)' }}>
                        Ingest warnings
                    </div>
                    {raster.warnings.map((w, i) => (
                        <div key={i} className="mb-0.5" style={{ color: 'var(--fg-2)' }}>
                            <span className="font-mono text-[10px]" style={{ color: 'var(--warn)' }}>
                                {w.code ?? 'warning'}
                            </span>{' '}
                            {w.message ?? ''}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
    return (
        <div className="min-w-0">
            <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                {label}
            </div>
            <div className={['truncate', mono ? 'font-mono text-[10px]' : ''].join(' ').trim()} style={{ color: 'var(--fg-1)' }} title={value}>
                {value}
            </div>
        </div>
    );
}

export default function FoundryRasterLayers({
    project,
    rasters,
    summary,
    ungeoreferenced,
}: RasterLayersProps) {
    const [selectedId, setSelectedId] = useState<string | null>(null);

    const empty = rasters.length === 0 && ungeoreferenced.length === 0;

    return (
        <AppLayout>
            <Head title={`Rasters · ${project.project_name}`} />

            <div className="flex-1 overflow-y-auto" style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}>
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · RASTERS`}
                    title="Raster layers"
                    sub={
                        <span>
                            {summary.total} indexed
                            {summary.missing_crs > 0 && ` · ${summary.missing_crs} without a CRS`}
                            {summary.ocr_skipped > 0 && ` · ${summary.ocr_skipped} not sent to OCR`}
                        </span>
                    }
                    actions={
                        <Link
                            href={`/projects/${project.slug}/ingestion-runs`}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                        >
                            Ingestion runs
                        </Link>
                    }
                />

                {/* The honesty banner. It is above the map on purpose: the
                    footprint rectangles are the part of this page most likely
                    to be mistaken for the image itself. */}
                <div className="px-8 pt-5">
                    <div
                        className="rounded-md border px-4 py-3 text-xs"
                        style={{ borderColor: 'var(--line-2)', background: 'var(--bg-1)', color: 'var(--fg-2)' }}
                    >
                        <strong style={{ color: 'var(--fg-0)' }}>Indexed, not yet viewable.</strong>{' '}
                        These rasters are catalogued — extent, resolution, bands, CRS — but their
                        pixels are not served to the browser. There is no tiled or cloud-optimized
                        copy, and the original file sits in object storage where a web page cannot
                        reach it. The map below draws each raster&rsquo;s <em>footprint</em>: the
                        ground it covers, not the image.
                    </div>
                </div>

                {empty && (
                    <div className="px-8 py-6">
                        <EmptyState
                            title="No rasters indexed for this project"
                            detail={
                                <>
                                    A raster is recorded when a georeferenced file (GeoTIFF, NetCDF,
                                    ASCII grid, JPEG2000) is ingested and its header carries a CRS.
                                    Nothing here means either nothing like that has been uploaded, or
                                    what was uploaded arrived with no coordinate system at all.
                                </>
                            }
                            action={
                                <Link
                                    href={`/projects/${project.slug}/sources`}
                                    className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{ color: 'var(--accent)', borderColor: 'var(--accent)' }}
                                >
                                    ↑ Upload files →
                                </Link>
                            }
                        />
                    </div>
                )}

                {rasters.length > 0 && (
                    <>
                        <section className="px-8 pt-5">
                            <div
                                className="grid grid-cols-2 md:grid-cols-5 gap-px rounded-md overflow-hidden border"
                                style={{ background: 'var(--line-1)', borderColor: 'var(--line-1)' }}
                            >
                                <Stat label="Indexed" value={summary.total} title="Rasters with a row in silver.raster_layers for this project." />
                                <Stat
                                    label="Georeferenced"
                                    value={summary.georeferenced}
                                    tone="accent"
                                    title="Rasters carrying a CRS. Only these can be placed, clipped or overlaid."
                                />
                                <Stat
                                    label="No footprint"
                                    value={summary.missing_footprint}
                                    tone={summary.missing_footprint > 0 ? 'warn' : 'neutral'}
                                    title="Indexed, but their bounds could not be reprojected to WGS84 — nothing to draw on a map."
                                />
                                <Stat
                                    label="Not OCR'd"
                                    value={summary.ocr_skipped}
                                    title="Measurement grids (DEM, magnetics). Ingest skipped OCR deliberately, so these files have no document and cannot be found in chat."
                                />
                                <Stat
                                    label="With warnings"
                                    value={summary.with_warnings}
                                    tone={summary.with_warnings > 0 ? 'warn' : 'neutral'}
                                    title="Rasters the parser flagged during ingest — expand a row to read them."
                                />
                            </div>
                        </section>

                        <section className="px-8 py-5">
                            <Card eyebrow="FOOTPRINTS" title="Where these rasters are" padded={false}>
                                <FootprintMap
                                    rasters={rasters}
                                    selectedId={selectedId}
                                    onSelect={(id) => setSelectedId((cur) => (cur === id ? null : id))}
                                />
                            </Card>
                        </section>

                        <section className="px-8 pb-5">
                            <Card eyebrow={`LAYERS · ${rasters.length}`} title="Indexed rasters" padded={false}>
                                <div className="overflow-x-auto">
                                    <div className="min-w-[760px]">
                                        <div
                                            className="grid grid-cols-[1.6fr_140px_1fr_120px_120px] text-[10px] font-mono uppercase tracking-wider px-4 py-2 border-b"
                                            style={{ color: 'var(--fg-3)', borderColor: 'var(--line-1)' }}
                                        >
                                            <div>Layer</div>
                                            <div>Size</div>
                                            <div>CRS</div>
                                            <div>Bands</div>
                                            <div>State</div>
                                        </div>

                                        {rasters.map((r) => (
                                            <div key={r.raster_id}>
                                                <button
                                                    type="button"
                                                    onClick={() =>
                                                        setSelectedId((cur) => (cur === r.raster_id ? null : r.raster_id))
                                                    }
                                                    className="w-full text-left grid grid-cols-[1.6fr_140px_1fr_120px_120px] text-xs px-4 py-3 border-b items-center gap-4"
                                                    style={{
                                                        borderColor: 'var(--line-1)',
                                                        background:
                                                            selectedId === r.raster_id ? 'var(--bg-2)' : 'transparent',
                                                    }}
                                                >
                                                    <div className="min-w-0">
                                                        <div className="truncate" style={{ color: 'var(--fg-0)' }} title={r.layer_name}>
                                                            {r.layer_name}
                                                        </div>
                                                        <div
                                                            className="truncate font-mono text-[10px]"
                                                            style={{ color: 'var(--fg-3)' }}
                                                            title={r.source_filename ?? r.source_file}
                                                        >
                                                            {r.source_filename ?? r.source_file}
                                                        </div>
                                                    </div>
                                                    <div className="font-mono text-[10px] tabular-nums" style={{ color: 'var(--fg-2)' }}>
                                                        {r.width.toLocaleString()} x {r.height.toLocaleString()}
                                                        <div style={{ color: 'var(--fg-3)' }}>{megapixels(r.width, r.height)}</div>
                                                    </div>
                                                    <div className="min-w-0 font-mono text-[10px]" style={{ color: 'var(--fg-2)' }}>
                                                        {r.crs ? (
                                                            <span className="truncate block" title={r.crs}>
                                                                {r.crs}
                                                            </span>
                                                        ) : (
                                                            <span style={{ color: 'var(--warn)' }}>none</span>
                                                        )}
                                                    </div>
                                                    <div className="font-mono text-[10px] tabular-nums" style={{ color: 'var(--fg-2)' }}>
                                                        {r.band_count}
                                                    </div>
                                                    <div className="flex flex-wrap gap-1">
                                                        {!r.georeferenced && <Pill tone="warn">needs georef</Pill>}
                                                        {r.georeferenced && !r.bounds && <Pill tone="warn">no footprint</Pill>}
                                                        {r.ocr_skipped && <Pill tone="info">no text</Pill>}
                                                        {r.is_cog && <Pill tone="accent">cog</Pill>}
                                                        {r.warning_count > 0 && <Pill tone="warn">{r.warning_count}⚠</Pill>}
                                                    </div>
                                                </button>
                                                {selectedId === r.raster_id && <RasterDetail raster={r} />}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </Card>

                            {summary.truncated && (
                                <div className="text-[10px] font-mono mt-2" style={{ color: 'var(--fg-3)' }}>
                                    Showing the {summary.list_limit} most recent rasters. The counts above
                                    describe this page, not the whole project.
                                </div>
                            )}
                        </section>
                    </>
                )}

                {ungeoreferenced.length > 0 && (
                    <section className="px-8 pb-8">
                        <Card
                            eyebrow={`NEEDS GEOREFERENCING · ${ungeoreferenced.length}`}
                            title="Images that arrived with no coordinate system"
                            padded={false}
                        >
                            <div className="px-4 py-3 text-[11px] border-b" style={{ borderColor: 'var(--line-1)', color: 'var(--fg-2)' }}>
                                These were ingested as pictures: the file carried no GeoTIFF keys (or a
                                header we could not read), so nothing was written to the raster
                                catalogue and there is no extent to put on the map. Their text was still
                                extracted and is searchable — but until somebody georeferences them, they
                                cannot be clipped to a claim, overlaid on a survey, or answered
                                spatially. On one real delivery this was 5 of 10 TIFFs.
                            </div>
                            {ungeoreferenced.map((t) => (
                                <div
                                    key={t.report_id}
                                    className="grid grid-cols-[1.6fr_1fr] text-xs px-4 py-2.5 border-b gap-4 items-center"
                                    style={{ borderColor: 'var(--line-1)' }}
                                >
                                    <div className="min-w-0">
                                        <div className="truncate font-mono text-[11px]" style={{ color: 'var(--fg-1)' }} title={t.source_filename ?? ''}>
                                            {t.source_filename ?? '(name not recoverable)'}
                                        </div>
                                        <div className="truncate text-[10px]" style={{ color: 'var(--fg-3)' }} title={t.title}>
                                            {t.title}
                                        </div>
                                    </div>
                                    <Link
                                        href={`/projects/${project.slug}/reports/${t.report_id}`}
                                        className="justify-self-start text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                    >
                                        Open document
                                    </Link>
                                </div>
                            ))}
                        </Card>
                    </section>
                )}
            </div>
        </AppLayout>
    );
}
