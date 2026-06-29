import { useState, useEffect, useCallback } from 'react';
import type { CollarRecord } from '@/types';
import { cn } from '../lib/utils';

/**
 * DrillHoleBrowser
 *
 * Filterable, sortable table of drill holes for a project.
 * Fetches from GET /api/v1/projects/{projectId}/collars.
 *
 * Props:
 *   projectId    {string}   - UUID of the active project
 *   onHoleClick  {function} - callback(hole_id) when a row is selected
 *   selectedHoleId {string} - currently selected hole_id (controlled)
 */

const HOLE_TYPE_OPTIONS = ['All', 'Diamond', 'RC', 'RAB'];
const STATUS_OPTIONS    = ['All', 'Completed', 'Active', 'Abandoned'];

const STATUS_BADGE = {
    Completed: 'bg-green-900/60 text-green-300 border-green-700/50',
    Active:    'bg-yellow-900/60 text-yellow-300 border-yellow-700/50',
    Abandoned: 'bg-red-900/60 text-red-300 border-red-700/50',
};

function StatusBadge({ status }) {
    const cls = STATUS_BADGE[status] ?? 'bg-gray-800 text-gray-400 border-gray-700';
    return (
        <span className={cn(
            'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border',
            cls,
        )}>
            {status ?? '—'}
        </span>
    );
}

function SortIcon({ direction }) {
    if (!direction) {
        return (
            <svg className="w-3 h-3 text-gray-600 ml-1 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                <path d="M7 15l5 5 5-5M7 9l5-5 5 5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
        );
    }
    return (
        <svg className="w-3 h-3 text-amber-400 ml-1 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
            {direction === 'asc'
                ? <path d="M7 14l5-5 5 5" strokeLinecap="round" strokeLinejoin="round" />
                : <path d="M7 10l5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
            }
        </svg>
    );
}

export default function DrillHoleBrowser({ projectId, onHoleClick, selectedHoleId }) {
    const [collars, setCollars]       = useState<CollarRecord[]>([]);
    const [loading, setLoading]       = useState(false);
    const [error, setError]           = useState<string | null>(null);

    // Filter state
    const [holeTypeFilter, setHoleTypeFilter] = useState('All');
    const [statusFilter, setStatusFilter]     = useState('All');
    const [searchText, setSearchText]         = useState('');

    // Sort state: { column: string, direction: 'asc'|'desc' }
    const [sort, setSort] = useState({ column: 'hole_id', direction: 'asc' });

    const fetchCollars = useCallback(async () => {
        if (!projectId) return;

        setLoading(true);
        setError(null);

        try {
            const params = new URLSearchParams({ per_page: '200' });
            if (holeTypeFilter !== 'All') params.set('hole_type', holeTypeFilter);
            if (statusFilter !== 'All')   params.set('status', statusFilter);

            // Auth via Sanctum session cookie (same-origin). No bearer token from
            // localStorage — localStorage is an XSS-exfiltration target (types.ts:11-12).
            const res = await fetch(
                `/api/v1/projects/${projectId}/collars?${params}`,
                {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                },
            );

            if (!res.ok) throw new Error(`HTTP ${res.status}`);

            const body = await res.json();
            setCollars(body.data ?? body);
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setLoading(false);
        }
    }, [projectId, holeTypeFilter, statusFilter]);

    useEffect(() => {
        fetchCollars();
    }, [fetchCollars]);

    // Client-side text search on hole_id
    const filtered = collars.filter((c) =>
        !searchText || c.hole_id.toLowerCase().includes(searchText.toLowerCase()),
    );

    // Client-side sort
    const sorted = [...filtered].sort((a, b) => {
        const { column, direction } = sort;
        let av = a[column] ?? '';
        let bv = b[column] ?? '';

        if (column === 'total_depth') {
            av = parseFloat(av) || 0;
            bv = parseFloat(bv) || 0;
            return direction === 'asc' ? av - bv : bv - av;
        }

        av = String(av).toLowerCase();
        bv = String(bv).toLowerCase();
        if (av < bv) return direction === 'asc' ? -1 : 1;
        if (av > bv) return direction === 'asc' ? 1 : -1;
        return 0;
    });

    function handleSortClick(column) {
        setSort((prev) =>
            prev.column === column
                ? { column, direction: prev.direction === 'asc' ? 'desc' : 'asc' }
                : { column, direction: 'asc' },
        );
    }

    function thClass(column) {
        return cn(
            'text-left text-xs font-medium text-gray-400 uppercase tracking-wider px-3 py-2 cursor-pointer select-none',
            'hover:text-gray-200 transition-colors duration-100',
        );
    }

    // --- Loading state ---
    if (!projectId) {
        return (
            <div className="flex-1 flex items-center justify-center text-sm text-gray-500 px-4">
                Select a project to view drill holes.
            </div>
        );
    }

    return (
        <div className="flex flex-col h-full bg-gray-950">
            {/* Panel header */}
            <div className="px-3 pt-3 pb-2 border-b border-gray-800 space-y-2 shrink-0">
                <h2 className="text-xs font-semibold text-gray-300 uppercase tracking-wider">
                    Drill Holes
                </h2>

                {/* Search */}
                <input
                    type="search"
                    value={searchText}
                    onChange={(e) => setSearchText(e.target.value)}
                    placeholder="Search hole ID…"
                    className={cn(
                        'w-full bg-gray-800 text-gray-100 placeholder-gray-500 text-xs',
                        'border border-gray-700 rounded px-2.5 py-1.5',
                        'focus:outline-none focus:ring-1 focus:ring-amber-500 focus:border-transparent',
                    )}
                    aria-label="Search drill holes by hole ID"
                />

                {/* Filter row */}
                <div className="flex gap-2">
                    <select
                        value={holeTypeFilter}
                        onChange={(e) => setHoleTypeFilter(e.target.value)}
                        className={cn(
                            'flex-1 bg-gray-800 text-gray-300 text-xs border border-gray-700 rounded',
                            'px-2 py-1 focus:outline-none focus:ring-1 focus:ring-amber-500 cursor-pointer',
                        )}
                        aria-label="Filter by hole type"
                    >
                        {HOLE_TYPE_OPTIONS.map((t) => (
                            <option key={t} value={t}>{t === 'All' ? 'All types' : t}</option>
                        ))}
                    </select>

                    <select
                        value={statusFilter}
                        onChange={(e) => setStatusFilter(e.target.value)}
                        className={cn(
                            'flex-1 bg-gray-800 text-gray-300 text-xs border border-gray-700 rounded',
                            'px-2 py-1 focus:outline-none focus:ring-1 focus:ring-amber-500 cursor-pointer',
                        )}
                        aria-label="Filter by status"
                    >
                        {STATUS_OPTIONS.map((s) => (
                            <option key={s} value={s}>{s === 'All' ? 'All statuses' : s}</option>
                        ))}
                    </select>
                </div>
            </div>

            {/* Results count */}
            <div className="px-3 py-1.5 shrink-0">
                <span className="text-xs text-gray-500">
                    {loading ? 'Loading…' : `${sorted.length} hole${sorted.length !== 1 ? 's' : ''}`}
                </span>
            </div>

            {/* Error state */}
            {error && (
                <div className="mx-3 mb-2 text-xs text-red-400 bg-red-950/40 border border-red-800/40 rounded px-3 py-2" role="alert">
                    Failed to load collars: {error}
                </div>
            )}

            {/* Table scroll area */}
            <div className="flex-1 overflow-y-auto min-h-0">
                {loading && collars.length === 0 ? (
                    <div className="flex items-center justify-center py-12">
                        <div className="w-4 h-4 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin" />
                    </div>
                ) : sorted.length === 0 ? (
                    <div className="px-3 py-8 text-center text-xs text-gray-500">
                        No drill holes match the current filters.
                    </div>
                ) : (
                    <table className="w-full text-xs" role="grid" aria-label="Drill holes">
                        <thead className="sticky top-0 bg-gray-900 z-10">
                            <tr>
                                <th
                                    scope="col"
                                    className={thClass('hole_id')}
                                    onClick={() => handleSortClick('hole_id')}
                                    aria-sort={sort.column === 'hole_id' ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}
                                >
                                    <span className="flex items-center">
                                        Hole ID
                                        <SortIcon direction={sort.column === 'hole_id' ? sort.direction : null} />
                                    </span>
                                </th>
                                <th
                                    scope="col"
                                    className={thClass('hole_type')}
                                    onClick={() => handleSortClick('hole_type')}
                                    aria-sort={sort.column === 'hole_type' ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}
                                >
                                    <span className="flex items-center">
                                        Type
                                        <SortIcon direction={sort.column === 'hole_type' ? sort.direction : null} />
                                    </span>
                                </th>
                                <th
                                    scope="col"
                                    className={cn(thClass('total_depth'), 'text-right')}
                                    onClick={() => handleSortClick('total_depth')}
                                    aria-sort={sort.column === 'total_depth' ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}
                                >
                                    <span className="flex items-center justify-end">
                                        Depth
                                        <SortIcon direction={sort.column === 'total_depth' ? sort.direction : null} />
                                    </span>
                                </th>
                                <th scope="col" className={thClass('status')}>
                                    Status
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            {sorted.map((collar) => {
                                const isSelected = collar.hole_id === selectedHoleId;
                                return (
                                    <tr
                                        key={collar.collar_id}
                                        onClick={() => onHoleClick?.(collar.hole_id)}
                                        className={cn(
                                            'border-b border-gray-800/60 cursor-pointer transition-colors duration-100',
                                            isSelected
                                                ? 'bg-amber-950/40 border-l-2 border-l-amber-500'
                                                : 'hover:bg-gray-800/50',
                                        )}
                                        role="row"
                                        aria-selected={isSelected}
                                        tabIndex={0}
                                        onKeyDown={(e) => {
                                            if (e.key === 'Enter' || e.key === ' ') {
                                                e.preventDefault();
                                                onHoleClick?.(collar.hole_id);
                                            }
                                        }}
                                    >
                                        <td className="px-3 py-2 font-mono text-gray-100 whitespace-nowrap">
                                            {collar.hole_id}
                                        </td>
                                        <td className="px-3 py-2 text-gray-400">
                                            {collar.hole_type ?? '—'}
                                        </td>
                                        <td className="px-3 py-2 text-right text-gray-300 font-mono tabular-nums">
                                            {collar.total_depth != null
                                                ? `${collar.total_depth.toFixed(1)} m`
                                                : '—'}
                                        </td>
                                        <td className="px-3 py-2">
                                            <StatusBadge status={collar.status} />
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                )}
            </div>
        </div>
    );
}
