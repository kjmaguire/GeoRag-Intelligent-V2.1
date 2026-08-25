/**
 * Renders an extracted document body: prose as prose, recovered tables as
 * tables.
 *
 * The reader used to print the whole body with `whitespace-pre-wrap`, so a
 * table the PDF stack had correctly recovered arrived as a wall of escaped
 * `<tr>`/`<td>` markup or raw markdown pipes. The parsing lives in
 * `@/lib/documentBlocks`; this file is only presentation.
 *
 * Nothing here uses `dangerouslySetInnerHTML`. Cell text arrives as plain
 * strings and is rendered as React children, which is what keeps content from
 * an uploaded PDF out of the DOM as markup.
 */
import { useMemo, type ReactElement } from 'react';

import {
    emptyColumns,
    parseDocumentBlocks,
    tableWidth,
    type Cell,
    type TableBlock,
} from '@/lib/documentBlocks';

/** A cell that is a bare number gets tabular figures so columns line up. */
function isNumeric(text: string): boolean {
    return /^[-+]?[\d.,]+%?$/.test(text.trim()) && /\d/.test(text);
}

function RecoveredTable({ block }: { block: TableBlock }) {
    // OCR'd tables carry columns that are empty in every row — an artefact of
    // the layout model splitting on whitespace. Dropping them is the single
    // biggest readability win on a real one.
    const dropped = useMemo(() => emptyColumns(block), [block]);
    const width = tableWidth(block);

    const renderRow = (row: Cell[], rowKey: string, header: boolean) => {
        const out: ReactElement[] = [];
        let col = 0;
        for (const [i, cell] of row.entries()) {
            const span = cell.colSpan;
            // A cell whose every underlying column was dropped goes with them.
            let visible = 0;
            for (let k = 0; k < span; k++) if (!dropped.has(col + k)) visible += 1;
            if (visible > 0) {
                const Tag = header ? 'th' : 'td';
                out.push(
                    <Tag
                        key={`${rowKey}-${i}`}
                        colSpan={visible > 1 ? visible : undefined}
                        className={[
                            'px-2 py-1 align-top border',
                            header ? 'font-medium text-left' : '',
                            isNumeric(cell.text) ? 'text-right tabular-nums' : 'text-left',
                        ].join(' ')}
                        style={{
                            borderColor: 'var(--line-1)',
                            color: header ? 'var(--fg-0)' : 'var(--fg-1)',
                            background: header ? 'var(--bg-2)' : 'transparent',
                            whiteSpace: 'nowrap',
                        }}
                    >
                        {cell.text || ' '}
                    </Tag>,
                );
            }
            col += span;
        }
        return out;
    };

    const visibleWidth = width - dropped.size;

    return (
        <div className="my-3">
            {/* Wide tables scroll inside their own box; the page itself must
                never scroll sideways. */}
            <div className="overflow-x-auto rounded border" style={{ borderColor: 'var(--line-1)' }}>
                <table
                    className="text-[12px] border-collapse w-full"
                    style={{ fontFamily: 'var(--font-mono)' }}
                >
                    {block.head.length > 0 && (
                        <thead>
                            {block.head.map((row, r) => (
                                <tr key={`h${r}`}>{renderRow(row, `h${r}`, true)}</tr>
                            ))}
                        </thead>
                    )}
                    <tbody>
                        {block.body.map((row, r) => (
                            <tr key={`b${r}`}>{renderRow(row, `b${r}`, false)}</tr>
                        ))}
                    </tbody>
                </table>
            </div>
            <div
                className="text-[10px] font-mono uppercase tracking-wider mt-1"
                style={{ color: 'var(--fg-3)' }}
            >
                recovered table · {block.body.length} row
                {block.body.length === 1 ? '' : 's'} · {visibleWidth} col
                {visibleWidth === 1 ? '' : 's'}
                {dropped.size > 0 ? ` · ${dropped.size} empty column${dropped.size === 1 ? '' : 's'} hidden` : ''}
            </div>
        </div>
    );
}

export default function DocumentBody({ body }: { body: string }) {
    const blocks = useMemo(() => parseDocumentBlocks(body), [body]);

    if (blocks.length === 0) {
        return (
            <span className="italic" style={{ color: 'var(--fg-3)' }}>
                (empty body)
            </span>
        );
    }

    return (
        <>
            {blocks.map((block, i) =>
                block.kind === 'table' ? (
                    <RecoveredTable key={i} block={block} />
                ) : (
                    <p
                        key={i}
                        className="text-[13px] whitespace-pre-wrap leading-relaxed my-2"
                        style={{ color: 'var(--fg-1)', fontFamily: 'var(--font-sans)' }}
                    >
                        {block.text}
                    </p>
                ),
            )}
        </>
    );
}
