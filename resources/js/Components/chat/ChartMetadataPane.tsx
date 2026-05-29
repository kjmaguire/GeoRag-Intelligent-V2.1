import type { JSX } from 'react';
import { useState } from 'react';
import { Info } from 'lucide-react';

/**
 * ChartMetadataPane — §17.4 chart export contract.
 *
 * Renders the 6-field ChartExportPayload that every chart MUST carry
 * (per §17.4):
 *   - source_data
 *   - method
 *   - filters
 *   - crs
 *   - citations
 *   - confidence_warnings
 *
 * Click to expand. Drop into any chart viewer (InlineViz,
 * StripLogViewer, the §5 cross-section + stereonet pages, etc.).
 */

export type ChartExportPayload = {
    source_data: string;
    method: string;
    filters: Record<string, unknown>;
    crs: string;
    citations: string[];
    confidence_warnings: string[];
};

type Props = {
    metadata: ChartExportPayload | null | undefined;
    exhibit_id?: string | null;
};

export function ChartMetadataPane({ metadata, exhibit_id }: Props): JSX.Element | null {
    const [expanded, setExpanded] = useState<boolean>(false);
    if (!metadata) return null;

    return (
        <div className="border-t border-gray-800 bg-gray-950/60 text-xs">
            <button
                type="button"
                onClick={() => setExpanded(!expanded)}
                className="w-full flex items-center justify-between px-3 py-1.5 text-gray-400 hover:text-gray-200"
            >
                <span className="flex items-center gap-1.5">
                    <Info className="w-3 h-3" />
                    Chart provenance (§17.4)
                    {exhibit_id && (
                        <span className="font-mono text-gray-500 ml-2">{exhibit_id}</span>
                    )}
                </span>
                <span>{expanded ? '−' : '+'}</span>
            </button>
            {expanded && (
                <div className="px-3 pb-3 space-y-1.5 text-gray-400">
                    <div>
                        <span className="text-gray-500">Source data:</span>{' '}
                        <span className="text-gray-300">{metadata.source_data}</span>
                    </div>
                    <div>
                        <span className="text-gray-500">Method:</span>{' '}
                        <span className="text-gray-300">{metadata.method}</span>
                    </div>
                    <div>
                        <span className="text-gray-500">CRS:</span>{' '}
                        <span className="text-gray-300 font-mono">{metadata.crs}</span>
                    </div>
                    {Object.keys(metadata.filters ?? {}).length > 0 && (
                        <div>
                            <span className="text-gray-500">Filters:</span>{' '}
                            <code className="text-gray-300 text-[10px]">
                                {JSON.stringify(metadata.filters)}
                            </code>
                        </div>
                    )}
                    {metadata.citations.length > 0 && (
                        <div>
                            <span className="text-gray-500">Citations:</span>{' '}
                            <span className="text-gray-300">
                                {metadata.citations.map(c => (
                                    <code key={c} className="mr-1 text-[10px]">
                                        {c}
                                    </code>
                                ))}
                            </span>
                        </div>
                    )}
                    {metadata.confidence_warnings.length > 0 && (
                        <div>
                            <span className="text-red-400">Confidence warnings:</span>{' '}
                            <ul className="ml-4 list-disc">
                                {metadata.confidence_warnings.map((w, i) => (
                                    <li key={i} className="text-red-300">{w}</li>
                                ))}
                            </ul>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

export default ChartMetadataPane;
