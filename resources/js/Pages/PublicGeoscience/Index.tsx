import { useCallback, useMemo, useRef, useState } from 'react';
import { Head, Link } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, Segmented } from '@/Components/Foundry/primitives';
import JurisdictionPicker from '@/Components/PublicGeoscience/JurisdictionPicker';
import LayerTogglePanel from '@/Components/PublicGeoscience/LayerTogglePanel';
import PublicGeoscienceMap, {
    type PublicGeoscienceMapHandle,
    type PointPopup,
    type BasemapId,
    type MapTool,
} from '@/Components/PublicGeoscience/PublicGeoscienceMap';
import FeaturePopupCard from '@/Components/PublicGeoscience/FeaturePopupCard';
import ExpandedFeatureCard from '@/Components/PublicGeoscience/ExpandedFeatureCard';
import CompareFeaturesModal from '@/Components/PublicGeoscience/CompareFeaturesModal';
import { useFullscreenToggle } from '@/Hooks/useFullscreenToggle';
import { useJurisdictions } from '@/Hooks/PublicGeoscience/useJurisdictions';
import type { Jurisdiction } from '@/Types/PublicGeoscience';
import {
    LAYER_SPECS,
    type LayerId,
} from '@/Components/PublicGeoscience/publicGeoscienceLayers';
import { cn } from '@/lib/utils';

/**
 * /public-geoscience — Foundry-shell rewrite (mirrors Pages/Foundry/Workspace.tsx).
 *
 * Three-column layout:
 *   left   — jurisdictions rail (JurisdictionPicker)
 *   center — Card-wrapped MapLibre canvas
 *   right  — layer toggles + commodity chips (LayerTogglePanel)
 *
 * Top chrome:
 *   PageHeader (eyebrow / title / sub / license action)
 *   Toolbar — Basemap segmented + active commodity Pill + counts
 *
 * Owns the cross-component state:
 *   - selectedCode       : which jurisdiction is active (also filters the map)
 *   - layerVisibility    : per-layer on/off toggles
 *   - commodityGrouping  : commodity filter chip (null = all)
 *   - activePopup        : feature the user just clicked (detail card)
 *   - basemap            : MapLibre basemap (default dark_matter, matches Workspace)
 */

function initialLayerVisibility(): Record<LayerId, boolean> {
    const out: Partial<Record<LayerId, boolean>> = {};
    for (const spec of LAYER_SPECS) out[spec.id] = spec.defaultVisible;
    return out as Record<LayerId, boolean>;
}

export default function PublicGeoscienceIndex() {
    const { data, loading, error, retry } = useJurisdictions();
    const [selectedCode, setSelectedCode] = useState<string | null>(null);
    const [layerVisibility, setLayerVisibility] = useState<Record<LayerId, boolean>>(
        initialLayerVisibility,
    );
    const [commodityGrouping, setCommodityGrouping] = useState<string | null>(null);
    const [activePopup, setActivePopup] = useState<PointPopup | null>(null);
    const [mobilePanel, setMobilePanel] = useState<'none' | 'jurisdictions' | 'layers'>('none');
    const [basemap, setBasemap] = useState<BasemapId>('dark_matter');
    const [activeTool, setActiveTool] = useState<MapTool>('pan');
    // Expanded-card + compare-queue state. The expanded card is just
    // the currently-displayed popup with `expanded === true`; cleaner
    // than a parallel state because clicking another feature should
    // both swap the compact popup AND close the expanded view.
    const [isExpanded, setIsExpanded] = useState<boolean>(false);
    const [compareSet, setCompareSet] = useState<PointPopup[]>([]);
    const [compareOpen, setCompareOpen] = useState<boolean>(false);
    const COMPARE_MAX = 3;
    // Fullscreen-within-app: hides PageHeader + top toolbar + both rails
    // and stretches the map's grid wrapper to fixed inset-0. Esc exits.
    const { isFullscreen: isMapFullscreen, toggle: toggleMapFullscreen } = useFullscreenToggle();

    const mapRef = useRef<PublicGeoscienceMapHandle | null>(null);

    const countries = useMemo(() => data?.countries ?? [], [data]);

    const selectedJurisdiction = useMemo<Jurisdiction | null>(() => {
        if (!selectedCode) return null;
        for (const country of countries) {
            const match = country.jurisdictions.find(
                (j) => j.jurisdiction_code === selectedCode,
            );
            if (match) return match;
        }
        return null;
    }, [selectedCode, countries]);

    // Auto-select on mount was removed intentionally — geologists asked to
    // see an empty Canada view until they pick a jurisdiction, so the
    // page doesn't burn tile bandwidth or visually overload before
    // they've made a choice. The empty-state overlay below the map
    // surfaces the "pick one to start" affordance.

    const handleSelect = useCallback((jurisdiction: Jurisdiction) => {
        setSelectedCode(jurisdiction.jurisdiction_code);
        setActivePopup(null); // clear any stale popup when jurisdiction changes
        setIsExpanded(false);
        if (jurisdiction.bbox) {
            mapRef.current?.fitBboxGeoJson(jurisdiction.bbox);
        }
    }, []);

    const handleToggleLayer = useCallback((id: LayerId) => {
        setLayerVisibility((prev) => ({ ...prev, [id]: !prev[id] }));
    }, []);

    const handleFeatureClick = useCallback((popup: PointPopup) => {
        setActivePopup(popup);
        // Clicking a different feature collapses the expanded view back
        // to the compact popup — the expanded panel always reflects the
        // currently-clicked feature, never a stale one.
        setIsExpanded(false);
    }, []);

    // Compare queue helpers — identify a feature by (layer, feature-id)
    // because cross-layer compare is allowed.
    const popupKey = useCallback((popup: PointPopup): string => {
        const id = popup.properties.source_feature_id
            ?? popup.properties.feature_id
            ?? popup.properties.smdi
            ?? popup.properties.drillhole_id
            ?? popup.properties.id
            ?? '';
        return `${popup.layerId}:${String(id)}`;
    }, []);

    const isInCompare = useCallback(
        (popup: PointPopup) => compareSet.some(p => popupKey(p) === popupKey(popup)),
        [compareSet, popupKey],
    );

    const toggleCompare = useCallback((popup: PointPopup) => {
        setCompareSet(prev => {
            const key = popupKey(popup);
            const without = prev.filter(p => popupKey(p) !== key);
            if (without.length !== prev.length) {
                // It was already queued — toggle removes.
                return without;
            }
            if (prev.length >= COMPARE_MAX) return prev;
            return [...prev, popup];
        });
    }, [popupKey]);

    const sourcesCount = selectedJurisdiction?.sources.length ?? 0;
    const headerSub = selectedJurisdiction
        ? `${selectedJurisdiction.primary_authority} · ${sourcesCount} source${sourcesCount === 1 ? '' : 's'}`
        : 'Government-published mineral, drillhole, and resource-potential data';

    return (
        <AppLayout>
            <Head title="Public Geoscience" />
            {/* Screen-reader live region — announces jurisdiction changes
                without stealing focus so blind users hear "Now showing
                Saskatchewan" when a tile is clicked. */}
            <div
                aria-live="polite"
                aria-atomic="true"
                className="sr-only"
            >
                {selectedJurisdiction
                    ? `Now showing ${selectedJurisdiction.display_name} public geoscience data`
                    : 'No jurisdiction selected'}
            </div>

            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <div className={cn(isMapFullscreen && 'hidden')}>
                <PageHeader
                    eyebrow={`PUBLIC GEOSCIENCE${selectedJurisdiction ? ` · ${selectedJurisdiction.jurisdiction_code}` : ''}`}
                    title={selectedJurisdiction?.display_name ?? 'Public geoscience'}
                    sub={headerSub}
                    actions={
                        selectedJurisdiction?.license_summary ? (
                            selectedJurisdiction.license_url ? (
                                <a
                                    href={selectedJurisdiction.license_url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                    style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
                                >
                                    License: {selectedJurisdiction.license_summary} ↗
                                </a>
                            ) : (
                                <span
                                    className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                    style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)' }}
                                >
                                    License: {selectedJurisdiction.license_summary}
                                </span>
                            )
                        ) : undefined
                    }
                />

                {/* Toolbar — basemap selector, mirrors Workspace's Mode/Tool toolbar.
                    Public Geo only has one mode (map), so Basemap takes the
                    primary slot instead. */}
                <div
                    className="flex items-center gap-3 px-8 py-2 border-b shrink-0"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                >
                    <span className="text-[10px] font-mono uppercase tracking-widest" style={{ color: 'var(--fg-3)' }}>
                        Basemap
                    </span>
                    <Segmented<BasemapId>
                        value={basemap}
                        onChange={setBasemap}
                        options={[
                            { value: 'dark_matter', label: 'Dark' },
                            { value: 'positron', label: 'Light' },
                            { value: 'bright', label: 'Bright' },
                            { value: 'satellite', label: 'Sat' },
                        ]}
                    />
                    {commodityGrouping && (
                        <>
                            <span className="ml-4 text-[10px] font-mono uppercase tracking-widest" style={{ color: 'var(--fg-3)' }}>
                                Commodity
                            </span>
                            <Pill tone="accent" dot>
                                {commodityGrouping.replace(/_/g, ' ')}
                            </Pill>
                            <button
                                type="button"
                                onClick={() => setCommodityGrouping(null)}
                                className="text-[10px] font-mono uppercase tracking-wider"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                clear ×
                            </button>
                        </>
                    )}
                    <div className="flex-1" />
                    {data && (
                        <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                            {data.counts.active} active · {data.counts.coming_soon} coming soon
                        </span>
                    )}
                    <Link
                        href="/public-geoscience/tier3-unlock"
                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                        style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}
                    >
                        Tier 3 access
                    </Link>
                </div>
                </div>{/* end PageHeader + Toolbar wrapper (fullscreen hides) */}

                {/* Mobile floating action buttons — visible on sm only,
                    toggle the jurisdiction picker or layer panel as an
                    overlay. Hidden on md+ where both rails render inline. */}
                <div className="md:hidden absolute top-32 left-3 z-30 flex flex-col gap-2">
                    <button
                        type="button"
                        onClick={() => setMobilePanel(v => v === 'jurisdictions' ? 'none' : 'jurisdictions')}
                        className="w-10 h-10 rounded-lg flex items-center justify-center shadow-lg border"
                        style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', color: 'var(--fg-2)' }}
                        aria-label="Toggle jurisdiction picker"
                    >
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5">
                            <path fillRule="evenodd" d="M9.69 18.933l.003.001C9.89 19.02 10 19 10 19s.11.02.308-.066l.002-.001.006-.003.018-.008a5.741 5.741 0 00.281-.14c.186-.096.446-.24.757-.433.62-.384 1.445-.966 2.274-1.765C15.302 14.988 17 12.493 17 9A7 7 0 103 9c0 3.492 1.698 5.988 3.355 7.584a13.731 13.731 0 002.273 1.765 11.842 11.842 0 00.976.544l.062.029.018.008.006.003zM10 11.25a2.25 2.25 0 100-4.5 2.25 2.25 0 000 4.5z" clipRule="evenodd" />
                        </svg>
                    </button>
                    <button
                        type="button"
                        onClick={() => setMobilePanel(v => v === 'layers' ? 'none' : 'layers')}
                        className="w-10 h-10 rounded-lg flex items-center justify-center shadow-lg border"
                        style={{ background: 'var(--bg-1)', borderColor: 'var(--line-2)', color: 'var(--fg-2)' }}
                        aria-label="Toggle layer controls"
                    >
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5">
                            <path d="M1 12.5A4.5 4.5 0 005.5 17H15a4 4 0 001.866-7.539 3.504 3.504 0 00-4.504-4.272A4.5 4.5 0 004.06 8.235 4.502 4.502 0 001 12.5z" />
                        </svg>
                    </button>
                </div>

                <div className={cn(
                    'overflow-hidden',
                    isMapFullscreen
                        ? 'fixed inset-0 z-[100] grid grid-cols-1'
                        : 'flex-1 grid grid-cols-[300px_1fr_300px]',
                )} style={isMapFullscreen ? { background: 'var(--bg-0)' } : undefined}>
                    {/* Left rail — jurisdictions */}
                    <aside
                        className={cn(
                            'border-r overflow-y-auto flex flex-col',
                            // Desktop: always visible
                            'hidden md:flex',
                            // Mobile: absolute overlay when toggled
                            mobilePanel === 'jurisdictions' && '!flex absolute inset-y-14 left-0 z-20 shadow-xl w-[300px]',
                            // Fullscreen hides the left rail outright
                            isMapFullscreen && '!hidden',
                        )}
                        style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                        aria-label="Jurisdiction picker"
                    >
                        <div
                            className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em] shrink-0"
                            style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
                        >
                            Jurisdictions
                        </div>
                        <div className="flex-1 overflow-hidden">
                            <JurisdictionPicker
                                countries={countries}
                                selectedCode={selectedCode}
                                onSelect={handleSelect}
                                loading={loading}
                                error={error}
                                onRetry={retry}
                            />
                        </div>
                    </aside>

                    {/* Center map — wrapped in a Foundry Card just like
                        Workspace's "MAP · MAPLIBRE · N COLLARS" panel.
                        The Card header's `actions` slot carries the
                        primary authority + license + source count so
                        that data stays visible permanently (it used to
                        live in a floating bottom-left chip on the map
                        and disappeared whenever a feature popup opened). */}
                    <section className={cn(
                        'flex flex-col overflow-hidden min-h-0',
                        isMapFullscreen ? 'p-0' : 'p-6',
                    )} aria-label="Map">
                        <Card
                            eyebrow={`MAP · MAPLIBRE${selectedJurisdiction ? ` · ${selectedJurisdiction.jurisdiction_code}` : ''}`}
                            title={
                                selectedJurisdiction
                                    ? `Layers for ${selectedJurisdiction.display_name}`
                                    : 'Pan + zoom to explore the public-geoscience corpus'
                            }
                            actions={(
                                <div className="flex items-start gap-2">
                                    {selectedJurisdiction && (
                                        <div className="text-right max-w-[360px]">
                                            <div className="text-xs font-medium truncate" style={{ color: 'var(--fg-0)' }} title={selectedJurisdiction.primary_authority}>
                                                {selectedJurisdiction.primary_authority}
                                            </div>
                                            <div className="text-[10px] font-mono mt-0.5 flex items-center justify-end gap-1.5 flex-wrap" style={{ color: 'var(--fg-3)' }}>
                                        {selectedJurisdiction.license_summary && (
                                            <>
                                                <span className="truncate" title={selectedJurisdiction.license_summary}>
                                                    {selectedJurisdiction.license_summary}
                                                </span>
                                                {selectedJurisdiction.license_url && (
                                                    <>
                                                        <span>·</span>
                                                        <a
                                                            href={selectedJurisdiction.license_url}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className="underline uppercase tracking-wider"
                                                            style={{ color: 'var(--accent)' }}
                                                        >
                                                            license ↗
                                                        </a>
                                                    </>
                                                )}
                                                <span>·</span>
                                            </>
                                        )}
                                        <span>{sourcesCount} source{sourcesCount === 1 ? '' : 's'}</span>
                                    </div>
                                        </div>
                                    )}
                                    <button
                                        type="button"
                                        onClick={toggleMapFullscreen}
                                        className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border shrink-0"
                                        style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                                        title={isMapFullscreen ? 'Exit fullscreen (Esc)' : 'Fullscreen map'}
                                    >
                                        {isMapFullscreen ? 'Exit ⤡' : 'Fullscreen ⤢'}
                                    </button>
                                </div>
                            )}
                            className="flex-1 flex flex-col min-h-0"
                            contentClassName="flex-1 flex flex-col min-h-0"
                        >
                            <div className="flex-1 min-h-0 relative rounded-md overflow-hidden" style={{ border: '1px solid var(--line-1)' }}>
                                <PublicGeoscienceMap
                                    ref={mapRef}
                                    /* Chip suppressed — PageHeader + Card title
                                       already show the active jurisdiction. */
                                    selectedLabel={null}
                                    jurisdictionCode={selectedCode}
                                    layerVisibility={layerVisibility}
                                    commodityGrouping={commodityGrouping}
                                    basemap={basemap}
                                    activeTool={activeTool}
                                    onToolChange={setActiveTool}
                                    onFeatureClick={handleFeatureClick}
                                />

                                {activePopup && !isExpanded && (
                                    <FeaturePopupCard
                                        popup={activePopup}
                                        onClose={() => setActivePopup(null)}
                                        onExpand={() => setIsExpanded(true)}
                                        onCompareToggle={() => toggleCompare(activePopup)}
                                        isInCompareSet={isInCompare(activePopup)}
                                        compareFull={compareSet.length >= COMPARE_MAX && !isInCompare(activePopup)}
                                    />
                                )}

                                {activePopup && isExpanded && (
                                    <ExpandedFeatureCard
                                        popup={activePopup}
                                        isInCompareSet={isInCompare(activePopup)}
                                        compareFull={compareSet.length >= COMPARE_MAX && !isInCompare(activePopup)}
                                        onClose={() => { setIsExpanded(false); setActivePopup(null); }}
                                        onCollapse={() => setIsExpanded(false)}
                                        onCompareToggle={() => toggleCompare(activePopup)}
                                    />
                                )}

                                {/* Compare queue banner — top-right of map,
                                    visible whenever ≥1 feature is queued.
                                    Mirrors WorkspaceMap's compare banner. */}
                                {compareSet.length > 0 && (
                                    <div
                                        className="absolute top-12 right-12 z-10 px-3 py-2 rounded border max-w-[260px]"
                                        style={{ background: 'var(--bg-1)', borderColor: '#e8a36b', color: 'var(--fg-1)' }}
                                    >
                                        <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: '#e8a36b' }}>
                                            Compare queue · {compareSet.length}/{COMPARE_MAX}
                                        </div>
                                        <div className="text-[11px] font-mono mb-2 leading-snug" style={{ color: 'var(--fg-1)' }}>
                                            {compareSet.length === 1
                                                ? <>1 feature queued · <span style={{ color: 'var(--fg-3)' }}>add another to compare</span></>
                                                : `${compareSet.length} features queued`}
                                        </div>
                                        <div className="flex gap-1">
                                            <button
                                                type="button"
                                                disabled={compareSet.length < 2}
                                                onClick={() => setCompareOpen(true)}
                                                className="flex-1 text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border disabled:opacity-40"
                                                style={{ color: '#e8a36b', borderColor: '#e8a36b', background: 'rgba(232,163,107,0.1)' }}
                                            >
                                                Open compare
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => setCompareSet([])}
                                                className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                                                style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)', background: 'var(--bg-2)' }}
                                            >
                                                Clear
                                            </button>
                                        </div>
                                    </div>
                                )}

                                {/* Empty-state overlay — shown until the
                                    user picks a jurisdiction. Pointer-events
                                    pass through so the user can still pan +
                                    zoom the empty basemap if they want. */}
                                {!selectedCode && !loading && (
                                    <div
                                        className="absolute inset-0 z-10 flex items-center justify-center pointer-events-none"
                                        aria-hidden="true"
                                    >
                                        <div
                                            className="text-center px-6 py-5 rounded-md border max-w-md"
                                            style={{
                                                background: 'color-mix(in oklch, var(--bg-1) 88%, transparent)',
                                                borderColor: 'var(--line-2)',
                                                color: 'var(--fg-1)',
                                                backdropFilter: 'blur(6px)',
                                            }}
                                        >
                                            <div className="text-[10px] font-mono uppercase tracking-[0.14em] mb-1" style={{ color: 'var(--fg-3)' }}>
                                                No jurisdiction selected
                                            </div>
                                            <div className="text-sm font-medium" style={{ color: 'var(--fg-0)' }}>
                                                Pick a jurisdiction to load data
                                            </div>
                                            <div className="text-[11px] mt-1.5 leading-snug" style={{ color: 'var(--fg-2)' }}>
                                                Expand <span className="font-mono" style={{ color: 'var(--fg-1)' }}>Canada</span> in the left rail and choose a province or territory. Mines, occurrences, drillholes, samples, and surveys will appear once a jurisdiction is active.
                                            </div>
                                        </div>
                                    </div>
                                )}

                                {/* The authority/license/source-count panel used
                                    to live here at bottom-left, hidden whenever
                                    a feature popup opened. It now lives
                                    permanently in the Card header's `actions`
                                    slot above the map so it never disappears. */}
                            </div>
                        </Card>
                    </section>

                    {/* Right rail — layer toggles (kept right, per user pick) */}
                    <aside
                        className={cn(
                            'border-l overflow-hidden flex flex-col',
                            'hidden md:flex',
                            mobilePanel === 'layers' && '!flex absolute inset-y-14 right-0 z-20 shadow-xl w-[300px]',
                            isMapFullscreen && '!hidden',
                        )}
                        style={{ borderColor: 'var(--line-1)', background: 'var(--bg-1)' }}
                        aria-label="Layer toggles"
                    >
                        <div
                            className="px-3 py-3 border-b text-[10px] font-mono uppercase tracking-[0.12em] shrink-0"
                            style={{ borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
                        >
                            Layers
                        </div>
                        <div className="flex-1 overflow-hidden">
                            <LayerTogglePanel
                                layerVisibility={layerVisibility}
                                onToggleLayer={handleToggleLayer}
                                commodityGrouping={commodityGrouping}
                                onCommoditySelect={setCommodityGrouping}
                            />
                        </div>
                    </aside>
                </div>
            </div>

            <CompareFeaturesModal
                open={compareOpen && compareSet.length >= 2}
                compareSet={compareSet}
                onClose={() => setCompareOpen(false)}
                onRemove={(p) => toggleCompare(p)}
                onClear={() => { setCompareSet([]); setCompareOpen(false); }}
            />
        </AppLayout>
    );
}
