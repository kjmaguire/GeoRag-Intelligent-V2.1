/**
 * Turning an extracted document body into things worth looking at.
 *
 * The §04p PDF stack emits a section body as mixed content: prose, plus
 * tables it recovered — as an HTML `<table>` from the layout model, or as a
 * markdown pipe table from the OCR table path. The reader rendered the whole
 * string with `whitespace-pre-wrap`, so a recovered table reached the
 * geologist as escaped markup:
 *
 *     <table> <tr> <th colspan="3">WHOLE ROCK</th> ...
 *
 * That is a table the pipeline got RIGHT, displayed as though it had failed.
 *
 * ## Why parse rather than inject
 *
 * The obvious fix — `dangerouslySetInnerHTML` — would put OCR output from an
 * uploaded file straight into the DOM. This content is untrusted by
 * definition: it comes from whatever PDF a user dropped on the import screen.
 * Instead the markup is parsed into plain arrays of strings and rendered as
 * React elements, so there is no path from document content to executable
 * markup at all. `DOMParser` is used only to read `textContent`; scripts in a
 * parsed document never run, and nothing from it is re-inserted.
 */

/** A run of prose. */
export interface TextBlock {
    kind: 'text';
    text: string;
}

/** A recovered table, already reduced to strings. */
export interface TableBlock {
    kind: 'table';
    /** Rows that came from `<th>` (or a markdown header), rendered as headers. */
    head: Cell[][];
    body: Cell[][];
}

export interface Cell {
    text: string;
    /** Preserved so a merged header still spans its columns. */
    colSpan: number;
}

export type Block = TextBlock | TableBlock;

const HTML_TABLE_RE = /<table[\s>][\s\S]*?<\/table>/gi;

/** `| --- | :--: |` — the row that makes the line above it a header. */
const MD_DIVIDER_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cellsFromMarkdownRow(line: string): Cell[] {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|')) row = row.slice(0, -1);
    return row.split('|').map((text) => ({ text: text.trim(), colSpan: 1 }));
}

/**
 * A table is only worth promoting if it has more than one column somewhere.
 * A single-column "table" is a list, and rendering it with borders adds
 * ceremony without adding information.
 */
function isWorthRendering(head: Cell[][], body: Cell[][]): boolean {
    const widest = [...head, ...body].reduce(
        (n, row) => Math.max(n, row.reduce((w, c) => w + c.colSpan, 0)),
        0,
    );
    return widest > 1 && head.length + body.length > 1;
}

function parseHtmlTable(markup: string): TableBlock | null {
    if (typeof DOMParser === 'undefined') return null;

    let doc: Document;
    try {
        doc = new DOMParser().parseFromString(markup, 'text/html');
    } catch {
        return null;
    }
    const table = doc.querySelector('table');
    if (!table) return null;

    const head: Cell[][] = [];
    const body: Cell[][] = [];

    for (const tr of Array.from(table.querySelectorAll('tr'))) {
        const cells = Array.from(tr.querySelectorAll('th,td')).map((td) => ({
            // textContent only — never innerHTML. This is the boundary that
            // keeps document content out of the DOM as markup.
            text: (td.textContent ?? '').replace(/\s+/g, ' ').trim(),
            colSpan: Math.max(1, Number((td as HTMLTableCellElement).colSpan) || 1),
        }));
        if (cells.length === 0) continue;
        // A row of <th> is a header row wherever it appears: these tables
        // routinely carry a second, stacked header (a year under a category).
        const allHeaders = Array.from(tr.querySelectorAll('th')).length === cells.length;
        if (allHeaders && body.length === 0) {
            head.push(cells);
        } else {
            body.push(cells);
        }
    }

    if (!isWorthRendering(head, body)) return null;
    return { kind: 'table', head, body };
}

/**
 * Split a body into prose and tables, in document order.
 *
 * Returns a single text block when nothing table-shaped is found, so callers
 * can render the result unconditionally.
 */
export function parseDocumentBlocks(body: string): Block[] {
    if (!body || !body.trim()) return [];

    const blocks: Block[] = [];
    const pushText = (text: string) => {
        if (text.trim()) blocks.push({ kind: 'text', text: text.replace(/^\n+|\n+$/g, '') });
    };

    // --- HTML tables first, since they can contain pipe characters ---
    let cursor = 0;
    for (const match of body.matchAll(HTML_TABLE_RE)) {
        const at = match.index ?? 0;
        const parsed = parseHtmlTable(match[0]);
        if (!parsed) continue;
        pushText(body.slice(cursor, at));
        blocks.push(parsed);
        cursor = at + match[0].length;
    }
    const tail = body.slice(cursor);

    // --- markdown pipe tables in whatever prose is left ---
    const lines = tail.split('\n');
    let buffer: string[] = [];
    let i = 0;
    while (i < lines.length) {
        const isHeader =
            lines[i].includes('|') &&
            i + 1 < lines.length &&
            MD_DIVIDER_RE.test(lines[i + 1]) &&
            lines[i + 1].includes('-');

        if (!isHeader) {
            buffer.push(lines[i]);
            i += 1;
            continue;
        }

        const head = [cellsFromMarkdownRow(lines[i])];
        const rows: Cell[][] = [];
        i += 2; // skip the header and the divider
        while (i < lines.length && lines[i].includes('|')) {
            rows.push(cellsFromMarkdownRow(lines[i]));
            i += 1;
        }

        if (isWorthRendering(head, rows)) {
            pushText(buffer.join('\n'));
            buffer = [];
            blocks.push({ kind: 'table', head, body: rows });
        } else {
            // Not really a table — put it back verbatim rather than losing it.
            buffer.push(lines[i - rows.length - 2], lines[i - rows.length - 1]);
            for (const r of rows) buffer.push(r.map((c) => c.text).join(' | '));
        }
    }
    pushText(buffer.join('\n'));

    return blocks;
}

/**
 * Columns the table actually occupies, so every row can be padded to the same
 * width. A ragged OCR table otherwise renders with collapsing borders and
 * cells that do not line up under their headers.
 */
export function tableWidth(block: TableBlock): number {
    return [...block.head, ...block.body].reduce(
        (n, row) => Math.max(n, row.reduce((w, c) => w + c.colSpan, 0)),
        0,
    );
}

/**
 * Whether a column is empty in every row — OCR'd tables are full of these,
 * and dropping them is the single biggest readability win on a real one.
 */
export function emptyColumns(block: TableBlock): Set<number> {
    const width = tableWidth(block);
    const nonEmpty = new Set<number>();
    for (const row of [...block.head, ...block.body]) {
        let col = 0;
        for (const cell of row) {
            if (cell.text) {
                for (let k = 0; k < cell.colSpan; k++) nonEmpty.add(col + k);
            }
            col += cell.colSpan;
        }
    }
    const empty = new Set<number>();
    for (let c = 0; c < width; c++) if (!nonEmpty.has(c)) empty.add(c);
    return empty;
}
