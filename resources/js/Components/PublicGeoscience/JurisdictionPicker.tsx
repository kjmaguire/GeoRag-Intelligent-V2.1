import { useMemo, useState } from 'react';
import { Badge } from '@/Components/ui/badge';
import {
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from '@/Components/ui/tooltip';
import type { CountryGroup, Jurisdiction } from '@/Types/PublicGeoscience';
import { cn } from '@/lib/utils';

interface JurisdictionPickerProps {
    countries: CountryGroup[];
    selectedCode: string | null;
    onSelect: (jurisdiction: Jurisdiction) => void;
    loading: boolean;
    error: string | null;
    onRetry: () => void;
}

/**
 * Left-rail jurisdiction picker for the Public Geoscience surface.
 *
 * Renders one country card per `CountryGroup`, expandable to reveal its
 * jurisdiction tiles. Active tiles are clickable and pan the map to the
 * jurisdiction's bbox; `coming_soon` tiles are visually muted and carry a
 * one-line teaser plus a badge — no click handler attached (plan §09c).
 *
 * Two display normalisations applied client-side (no API change):
 *   1. Country groups whose code starts with "CA" (currently `CA` and
 *      `CAN` — the latter is a single Canada-federal sibling row, a
 *      historical data quirk) are merged into a single "Canada" wrapper.
 *      Future US groups stay separate.
 *   2. Jurisdictions inside each wrapper sort alphabetically by
 *      display_name so users can scan provinces top→bottom predictably.
 *
 * Default state: every wrapper collapsed. Geologists must click the
 * country to see the list — keeps the rail compact and gives an explicit
 * "I'm picking jurisdiction X" gesture (matched by the parent which only
 * loads MVT data after a jurisdiction is selected).
 */
export default function JurisdictionPicker({
    countries,
    selectedCode,
    onSelect,
    loading,
    error,
    onRetry,
}: JurisdictionPickerProps) {
    const [expanded, setExpanded] = useState<Record<string, boolean>>({});

    function toggle(countryCode: string) {
        setExpanded(prev => ({ ...prev, [countryCode]: !prev[countryCode] }));
    }

    // Merge all CA* groups into one "Canada" wrapper + alpha-sort
    // jurisdictions inside every wrapper. Memoised so we don't reshape
    // the list on every render.
    const displayCountries = useMemo<CountryGroup[]>(() => {
        const canadaGroups = countries.filter(c => /^CA/i.test(c.country_code));
        const otherGroups = countries.filter(c => !/^CA/i.test(c.country_code));

        const merged: CountryGroup[] = [];
        if (canadaGroups.length > 0) {
            // Dedupe by jurisdiction_code in case two backend groups
            // surface the same jurisdiction (e.g. CA-FED + CA-FEDERAL).
            const seen = new Set<string>();
            const jurisdictions: Jurisdiction[] = [];
            for (const g of canadaGroups) {
                for (const j of g.jurisdictions) {
                    if (seen.has(j.jurisdiction_code)) continue;
                    seen.add(j.jurisdiction_code);
                    jurisdictions.push(j);
                }
            }
            merged.push({
                country_code: 'CA',
                display_name: 'Canada',
                jurisdictions: jurisdictions.slice().sort((a, b) =>
                    a.display_name.localeCompare(b.display_name),
                ),
            });
        }
        for (const g of otherGroups) {
            merged.push({
                ...g,
                jurisdictions: g.jurisdictions.slice().sort((a, b) =>
                    a.display_name.localeCompare(b.display_name),
                ),
            });
        }
        return merged;
    }, [countries]);

    if (loading) {
        return (
            <div className="p-4 text-sm text-gray-500">
                Loading jurisdictions…
            </div>
        );
    }

    if (error) {
        return (
            <div className="p-4 text-sm text-red-400">
                <p className="mb-2">Failed to load jurisdictions: {error}</p>
                <button
                    type="button"
                    onClick={onRetry}
                    className="text-xs text-amber-400 hover:text-amber-300 underline"
                >
                    Retry
                </button>
            </div>
        );
    }

    if (displayCountries.length === 0) {
        return (
            <div className="p-4 text-sm text-gray-500">
                No jurisdictions registered yet.
            </div>
        );
    }

    return (
        <TooltipProvider delayDuration={200}>
            <div className="flex flex-col gap-3 p-3 overflow-y-auto">
                {displayCountries.map(country => (
                    <div
                        key={country.country_code}
                        className="rounded-lg border border-gray-800 bg-gray-900/50"
                    >
                        <button
                            type="button"
                            onClick={() => toggle(country.country_code)}
                            aria-expanded={!!expanded[country.country_code]}
                            className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-gray-900/80 rounded-t-lg"
                        >
                            <span className="text-sm font-semibold text-gray-100">
                                {country.display_name}
                            </span>
                            <span className="text-xs text-gray-500 font-mono">
                                {country.jurisdictions.length}
                            </span>
                        </button>

                        {expanded[country.country_code] && (
                            <ul
                                role="listbox"
                                aria-label={`${country.display_name} jurisdictions`}
                                className="flex flex-col gap-1 p-2 border-t border-gray-800"
                            >
                                {country.jurisdictions.map(j => (
                                    <JurisdictionTile
                                        key={j.jurisdiction_code}
                                        jurisdiction={j}
                                        selected={selectedCode === j.jurisdiction_code}
                                        onSelect={onSelect}
                                    />
                                ))}
                            </ul>
                        )}
                    </div>
                ))}
            </div>
        </TooltipProvider>
    );
}

// ── One tile ─────────────────────────────────────────────────────────────

interface JurisdictionTileProps {
    jurisdiction: Jurisdiction;
    selected: boolean;
    onSelect: (jurisdiction: Jurisdiction) => void;
}

function JurisdictionTile({
    jurisdiction,
    selected,
    onSelect,
}: JurisdictionTileProps) {
    const isActive = jurisdiction.status === 'active';
    const isComingSoon = jurisdiction.status === 'coming_soon';

    const base =
        'flex flex-col gap-1 px-3 py-2 rounded-md border text-left transition-colors';

    const tile = (
        <li
            role="option"
            aria-selected={selected}
            aria-disabled={!isActive}
        >
            <button
                type="button"
                disabled={!isActive}
                onClick={isActive ? () => onSelect(jurisdiction) : undefined}
                aria-disabled={!isActive}
                aria-label={`${jurisdiction.display_name}${isActive ? (selected ? ', selected' : '') : ', coming soon'}`}
                className={cn(
                    base,
                    'w-full focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-1 focus:ring-offset-gray-900',
                    isActive &&
                        (selected
                            ? 'border-amber-500 bg-amber-950/40 ring-1 ring-amber-500'
                            : 'border-gray-800 hover:border-amber-600 hover:bg-gray-900'),
                    isComingSoon &&
                        'border-gray-800 bg-gray-950/50 opacity-60 cursor-not-allowed',
                )}
            >
                <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-gray-100">
                        {jurisdiction.display_name}
                    </span>
                    {isActive && (
                        <Badge className="bg-amber-600/20 text-amber-300 border-amber-700">
                            Active
                        </Badge>
                    )}
                    {isComingSoon && (
                        <Badge className="bg-gray-800 text-gray-400 border-gray-700">
                            Coming Soon
                        </Badge>
                    )}
                </div>
                {jurisdiction.teaser && (
                    <span className="text-xs text-gray-500 leading-snug">
                        {jurisdiction.teaser}
                    </span>
                )}
            </button>
        </li>
    );

    // Wrap coming_soon tiles in a tooltip so users get the roadmap signal on
    // hover without a click action doing anything.
    if (isComingSoon) {
        return (
            <Tooltip>
                <TooltipTrigger asChild>{tile}</TooltipTrigger>
                <TooltipContent side="right">
                    Coming soon — not yet ingested into GeoRAG.
                </TooltipContent>
            </Tooltip>
        );
    }

    return tile;
}
