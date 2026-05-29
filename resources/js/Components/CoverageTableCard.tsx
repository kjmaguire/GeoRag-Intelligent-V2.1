import { useMemo } from 'react';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/Components/ui/table';

/**
 * CoverageTableCard — renders the `coverage_gap` intent payload (ADR-0007
 * PR-1). A shadcn `Table` of attribute coverage rows with an inline progress
 * bar per row, plus a header strip showing ingest-stage gap (bronze
 * ingest_manifest ↔ silver.reports ratio — §04g doc edit in ADR-0007).
 *
 * Lightweight, no Plotly: every value is a discrete number or pct.
 */

export interface CoverageRow {
    attribute: string;
    collars_with_data: number;
    collars_total: number;
    coverage_pct: number;
    notes?: string | null;
}

export interface IngestGap {
    indexed: number;
    processed: number;
    gap_pct: number;
}

interface CoverageTableCardProps {
    rows: CoverageRow[];
    ingestGap?: IngestGap | null;
    title?: string;
}

function barColor(pct: number): string {
    if (pct >= 75) return 'bg-emerald-500';
    if (pct >= 40) return 'bg-amber-500';
    return 'bg-red-500';
}

function fmtPct(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    return `${n.toFixed(1)}%`;
}

function fmtCount(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    return n.toLocaleString();
}

export default function CoverageTableCard({ rows, ingestGap, title }: CoverageTableCardProps) {
    const sortedRows = useMemo(
        () => (rows ? [...rows].sort((a, b) => a.coverage_pct - b.coverage_pct) : []),
        [rows],
    );

    if (!rows || rows.length === 0) {
        return (
            <div
                className="flex items-center justify-center p-6 text-xs text-gray-500"
                data-testid="coverage-empty"
            >
                No coverage data to display.
            </div>
        );
    }

    return (
        <div
            className="w-full overflow-y-auto bg-gray-950 text-gray-100"
            data-testid="coverage-table-card"
            aria-label={title || 'Coverage table'}
        >
            {ingestGap && (
                <div
                    className="px-4 py-3 border-b border-gray-800 bg-gray-900/60"
                    data-testid="coverage-ingest-gap"
                >
                    <div className="text-[11px] uppercase tracking-wider text-amber-400 font-semibold mb-1">
                        Ingest-stage gap
                    </div>
                    <div className="text-sm text-gray-200">
                        <span className="font-semibold">{fmtCount(ingestGap.processed)}</span>{' '}
                        of{' '}
                        <span className="font-semibold">{fmtCount(ingestGap.indexed)}</span>{' '}
                        indexed files processed —{' '}
                        <span className="text-red-400 font-semibold">
                            {fmtPct(ingestGap.gap_pct)} gap
                        </span>
                    </div>
                </div>
            )}

            <Table>
                <TableHeader>
                    <TableRow className="border-gray-800 hover:bg-transparent">
                        <TableHead className="text-gray-400">Attribute</TableHead>
                        <TableHead className="text-gray-400">Coverage</TableHead>
                        <TableHead className="text-gray-400 text-right whitespace-nowrap">
                            With data
                        </TableHead>
                        <TableHead className="text-gray-400 text-right whitespace-nowrap">
                            Total
                        </TableHead>
                        <TableHead className="text-gray-400 text-right whitespace-nowrap">
                            %
                        </TableHead>
                    </TableRow>
                </TableHeader>
                <TableBody>
                    {sortedRows.map((row) => {
                        const pct = Math.max(0, Math.min(100, row.coverage_pct ?? 0));
                        return (
                            <TableRow
                                key={row.attribute}
                                className="border-gray-800 hover:bg-gray-900/60"
                                data-testid={`coverage-row-${row.attribute}`}
                            >
                                <TableCell className="text-gray-100 font-medium">
                                    {row.attribute}
                                    {row.notes && (
                                        <div className="text-[11px] text-gray-500 mt-0.5">
                                            {row.notes}
                                        </div>
                                    )}
                                </TableCell>
                                <TableCell className="w-full min-w-[180px]">
                                    <div
                                        className="h-2 w-full rounded-full bg-gray-800 overflow-hidden"
                                        role="progressbar"
                                        aria-valuenow={pct}
                                        aria-valuemin={0}
                                        aria-valuemax={100}
                                        aria-label={`${row.attribute} coverage`}
                                    >
                                        <div
                                            className={`h-full ${barColor(pct)} transition-all`}
                                            style={{ width: `${pct}%` }}
                                        />
                                    </div>
                                </TableCell>
                                <TableCell className="text-right text-gray-300 tabular-nums">
                                    {fmtCount(row.collars_with_data)}
                                </TableCell>
                                <TableCell className="text-right text-gray-300 tabular-nums">
                                    {fmtCount(row.collars_total)}
                                </TableCell>
                                <TableCell className="text-right text-gray-100 font-semibold tabular-nums">
                                    {fmtPct(row.coverage_pct)}
                                </TableCell>
                            </TableRow>
                        );
                    })}
                </TableBody>
            </Table>
        </div>
    );
}
