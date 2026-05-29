// @ts-nocheck
import { useState, useCallback, useRef } from 'react';
import { Head, router } from '@inertiajs/react';
import AppLayout from '../Layouts/AppLayout';
import DrillHoleBrowser from '../Components/DrillHoleBrowser';
import HoleDetailSheet from '../Components/HoleDetailSheet';
import StripLogViewer from '../Components/StripLogViewer';
import MapView from '../Components/MapView';
import HoleAnalysisPanel from '../Components/HoleAnalysis/HoleAnalysisPanel';
import { cn } from '@/lib/utils';

/**
 * Explorer page — `/explorer`
 *
 * Three-panel geological data explorer:
 *   Left panel  (280px): DrillHoleBrowser — filterable collar list
 *   Center pane (flex):  Tabs switching between Map and Strip Log views
 *
 * Selecting a hole in the browser or by clicking a map marker:
 *   - Highlights the row in DrillHoleBrowser
 *   - Loads the strip log in the Strip Log tab (auto-switches to that tab)
 *   - Pans the map to that collar
 *
 * The "Ask GeoRAG" button in StripLogViewer navigates to /chat with a
 * pre-populated query via Inertia visit so chat state is preserved.
 */

interface TabDefinition {
    id: 'map' | 'striplog' | 'analysis';
    label: string;
}

const TABS: TabDefinition[] = [
    { id: 'map',      label: 'Map' },
    { id: 'striplog', label: 'Strip Log' },
    { id: 'analysis', label: 'Analysis' },
];

const SIDEBAR_MIN = 220;
const SIDEBAR_MAX = 600;
const SIDEBAR_DEFAULT = 340;

interface ExplorerProps {}

export default function Explorer(_props: ExplorerProps): JSX.Element {
    const [projectId, setProjectId]       = useState<string | null>(null);
    const [selectedHoleId, setSelectedHoleId] = useState<string | null>(null);
    const [sheetHoleId, setSheetHoleId]   = useState<string | null>(null);  // hole detail sheet
    const [activeTab, setActiveTab]       = useState<'map' | 'striplog' | 'analysis'>('map');
    const [sidebarWidth, setSidebarWidth] = useState<number>(SIDEBAR_DEFAULT);
    const [mobileSidebarOpen, setMobileSidebarOpen] = useState<boolean>(false);
    const dragging = useRef<boolean>(false);

    // ── Resize handle drag logic ─────────────────────────────────────────
    const handleMouseDown = useCallback((e: React.MouseEvent<HTMLDivElement>): void => {
        e.preventDefault();
        dragging.current = true;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        const startX = e.clientX;
        const startW = sidebarWidth;

        function onMouseMove(ev: MouseEvent): void {
            if (!dragging.current) return;
            const delta = ev.clientX - startX;
            const newW = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, startW + delta));
            setSidebarWidth(newW);
        }

        function onMouseUp(): void {
            dragging.current = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        }

        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
    }, [sidebarWidth]);

    const handleProjectChange = useCallback((id: string | null): void => {
        setProjectId(id);
        setSelectedHoleId(null);
    }, []);

    // When a hole is selected from the browser or map, open the detail sheet
    const handleHoleSelect = useCallback((holeId: string): void => {
        setSelectedHoleId(holeId);
        setSheetHoleId(holeId);
    }, []);

    // "Ask GeoRAG about this hole" — navigate to /chat
    // Inertia visit preserves the SPA feel; the chat page will see the URL.
    const handleQueryHole = useCallback((queryText: string): void => {
        router.visit('/chat', {
            method: 'get',
            data: { q: queryText },
            preserveState: false,
        });
    }, []);

    return (
        <AppLayout onProjectChange={handleProjectChange}>
            <Head title="Explorer" />

            <div className="flex flex-1 overflow-hidden h-full">

                {/* ── Mobile sidebar toggle ── */}
                <button
                    type="button"
                    onClick={() => setMobileSidebarOpen(!mobileSidebarOpen)}
                    className="sm:hidden absolute top-16 left-2 z-30 bg-gray-900 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-400 hover:text-gray-200 shadow-lg"
                >
                    {mobileSidebarOpen ? '✕ Close' : '☰ Holes'}
                </button>

                {/* ── Left panel: DrillHoleBrowser (resizable desktop, drawer mobile) ── */}
                <aside
                    className={cn(
                        'shrink-0 flex flex-col overflow-hidden',
                        // Desktop: inline resizable panel
                        'hidden sm:flex',
                    )}
                    style={{ width: sidebarWidth }}
                    aria-label="Drill hole browser"
                >
                    <DrillHoleBrowser
                        projectId={projectId}
                        onHoleClick={handleHoleSelect}
                        selectedHoleId={selectedHoleId}
                    />
                </aside>

                {/* Mobile drawer overlay */}
                {mobileSidebarOpen && (
                    <>
                        <div className="sm:hidden fixed inset-0 bg-black/50 z-20" onClick={() => setMobileSidebarOpen(false)} />
                        <aside className="sm:hidden fixed left-0 top-14 bottom-0 w-72 bg-gray-900 border-r border-gray-700 z-30 flex flex-col">
                            <DrillHoleBrowser
                                projectId={projectId}
                                onHoleClick={(holeId: string) => { handleHoleSelect(holeId); setMobileSidebarOpen(false); }}
                                selectedHoleId={selectedHoleId}
                            />
                        </aside>
                    </>
                )}

                {/* Resize drag handle (desktop only) */}
                <div
                    className="w-1.5 shrink-0 cursor-col-resize bg-gray-800 hover:bg-amber-600 active:bg-amber-500 transition-colors relative group hidden sm:block"
                    onMouseDown={handleMouseDown}
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Resize sidebar"
                    title="Drag to resize"
                >
                    <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 flex flex-col items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <span className="w-1 h-1 rounded-full bg-gray-400" />
                        <span className="w-1 h-1 rounded-full bg-gray-400" />
                        <span className="w-1 h-1 rounded-full bg-gray-400" />
                    </div>
                </div>

                {/* ── Center: Tabs + content ── */}
                <div className="flex-1 flex flex-col overflow-hidden min-w-0">

                    {/* Tab bar */}
                    <div
                        className="flex items-center gap-0.5 px-3 pt-2 pb-0 border-b border-gray-800 bg-gray-900 shrink-0"
                        role="tablist"
                        aria-label="View tabs"
                    >
                        {TABS.map((tab) => {
                            const isActive = activeTab === tab.id;
                            const isDisabled = (tab.id === 'striplog' || tab.id === 'analysis') && !selectedHoleId;

                            return (
                                <button
                                    key={tab.id}
                                    role="tab"
                                    aria-selected={isActive}
                                    aria-controls={`tabpanel-${tab.id}`}
                                    id={`tab-${tab.id}`}
                                    disabled={isDisabled}
                                    onClick={() => !isDisabled && setActiveTab(tab.id)}
                                    className={cn(
                                        'px-4 py-2 text-sm font-medium rounded-t transition-colors duration-150',
                                        'focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-inset',
                                        isActive
                                            ? 'text-amber-400 border-b-2 border-amber-500 bg-gray-950'
                                            : isDisabled
                                                ? 'text-gray-600 cursor-not-allowed'
                                                : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800',
                                    )}
                                    title={isDisabled ? 'Select a drill hole to view its strip log' : undefined}
                                >
                                    {tab.label}
                                    {tab.id === 'striplog' && selectedHoleId && (
                                        <span className="ml-2 text-xs font-mono text-amber-500/80">
                                            {selectedHoleId}
                                        </span>
                                    )}
                                </button>
                            );
                        })}

                        {/* Right side: hint text */}
                        <div className="ml-auto pb-2 pr-1">
                            {!selectedHoleId ? (
                                <span className="text-xs text-gray-600">
                                    Click a collar on the map or in the list to open its strip log
                                </span>
                            ) : (
                                <span className="text-xs text-gray-500 font-mono">
                                    {selectedHoleId}
                                </span>
                            )}
                        </div>
                    </div>

                    {/* Tab panels */}
                    <div className="flex-1 overflow-hidden relative">

                        {/* Map tab */}
                        <div
                            role="tabpanel"
                            id="tabpanel-map"
                            aria-labelledby="tab-map"
                            hidden={activeTab !== 'map'}
                            className={cn(
                                'absolute inset-0',
                                activeTab !== 'map' && 'pointer-events-none',
                            )}
                        >
                            <MapView
                                projectId={projectId}
                                onCollarClick={handleHoleSelect}
                                selectedHoleId={selectedHoleId}
                            />
                        </div>

                        {/* Strip Log tab */}
                        <div
                            role="tabpanel"
                            id="tabpanel-striplog"
                            aria-labelledby="tab-striplog"
                            hidden={activeTab !== 'striplog'}
                            className={cn(
                                'absolute inset-0 overflow-hidden',
                                activeTab !== 'striplog' && 'pointer-events-none',
                            )}
                        >
                            {selectedHoleId ? (
                                <StripLogViewer
                                    holeId={selectedHoleId}
                                    projectId={projectId}
                                    onQueryHole={handleQueryHole}
                                />
                            ) : (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center space-y-2">
                                        <p className="text-sm text-gray-500">
                                            No drill hole selected
                                        </p>
                                        <p className="text-xs text-gray-600">
                                            Select a hole from the browser or click a collar on the map.
                                        </p>
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Analysis tab — only render its contents when active,
                            so the lazy-loaded Plotly chart components don't
                            evaluate their module-level `createPlotlyComponent(
                            Plotly)` call until the user actually opens the
                            tab. Other tabs keep the hidden-div pattern because
                            they don't do Plotly top-level work. */}
                        <div
                            role="tabpanel"
                            id="tabpanel-analysis"
                            aria-labelledby="tab-analysis"
                            hidden={activeTab !== 'analysis'}
                            className={cn(
                                'absolute inset-0 overflow-hidden',
                                activeTab !== 'analysis' && 'pointer-events-none',
                            )}
                        >
                            {activeTab === 'analysis' && selectedHoleId && projectId ? (
                                <HoleAnalysisPanel
                                    holeId={selectedHoleId}
                                    projectId={projectId}
                                />
                            ) : activeTab === 'analysis' ? (
                                <div className="flex items-center justify-center h-full">
                                    <div className="text-center space-y-2">
                                        <p className="text-sm text-gray-500">
                                            No drill hole selected
                                        </p>
                                        <p className="text-xs text-gray-600">
                                            Select a hole from the browser or click a collar on the map.
                                        </p>
                                    </div>
                                </div>
                            ) : null}
                        </div>
                    </div>
                </div>
            </div>
            {/* ── Hole detail sheet (slide-over with inline chat) ── */}
            <HoleDetailSheet
                holeId={sheetHoleId}
                projectId={projectId}
                onClose={() => setSheetHoleId(null)}
                onNavigate={(holeId: string) => {
                    setSelectedHoleId(holeId);
                    setActiveTab('striplog');
                }}
            />
        </AppLayout>
    );
}
