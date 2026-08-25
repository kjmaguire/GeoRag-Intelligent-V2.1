/**
 * Fixtures are taken verbatim from a real ingested document — the 1983 Unga
 * Island whole-rock / age-date sample table, which reached the reader as a
 * wall of escaped <tr>/<td> markup because the body was printed with
 * `whitespace-pre-wrap`.
 */
import { describe, expect, it } from 'vitest';

import { emptyColumns, parseDocumentBlocks, tableWidth, type TableBlock } from '../documentBlocks';

const HTML_TABLE = `
<table>
<tr><th colspan="3">WHOLE ROCK</th><th>AND AGE</th><th colspan="4">DATE SAMPLE NUMBERS</th></tr>
<tr><th>1983</th><th></th><th>(TETON)</th><th>1982</th><th>(TETON)</th><th></th><th>1979</th><th>(ADGGS)</th></tr>
<tr><td>56032</td><td>WR</td><td></td><td>50849</td><td></td><td></td><td>614</td><td>Popof I</td></tr>
<tr><td>56033</td><td>WR</td><td></td><td>50851</td><td></td><td></td><td>615</td><td></td></tr>
</table>
`;

const MD_TABLE = [
    '| WHOLE ROCK | AND AGE | DATE SAMPLE NUMBERS |',
    '| --- | --- | --- |',
    '| 56032 | WR | 614 |',
    '| 56033 | WR | 615 |',
].join('\n');

const tables = (body: string) =>
    parseDocumentBlocks(body).filter((b): b is TableBlock => b.kind === 'table');

describe('parseDocumentBlocks — HTML tables', () => {
    it('recovers a table instead of leaving markup in the prose', () => {
        const found = tables(HTML_TABLE);

        expect(found).toHaveLength(1);
        expect(found[0].head).toHaveLength(2);
        expect(found[0].body).toHaveLength(2);
    });

    it('keeps colspan so a merged header still spans its columns', () => {
        const [t] = tables(HTML_TABLE);
        expect(t.head[0][0]).toEqual({ text: 'WHOLE ROCK', colSpan: 3 });
        expect(t.head[0][2]).toEqual({ text: 'DATE SAMPLE NUMBERS', colSpan: 4 });
    });

    it('reads cells as text, never as markup', () => {
        // The boundary that keeps content from an uploaded PDF out of the DOM.
        const [t] = tables('<table><tr><td><img src=x onerror=alert(1)>56032</td><td>WR</td></tr><tr><td>a</td><td>b</td></tr></table>');
        expect(t.body[0][0].text).toBe('56032');
        expect(t.body[0][0].text).not.toContain('<');
    });

    it('leaves surrounding prose in document order', () => {
        const blocks = parseDocumentBlocks(`Intro line.\n${HTML_TABLE}\nTrailing note.`);
        expect(blocks.map((b) => b.kind)).toEqual(['text', 'table', 'text']);
        expect((blocks[0] as { text: string }).text).toContain('Intro line.');
        expect((blocks[2] as { text: string }).text).toContain('Trailing note.');
    });

    it('does not promote a single-column table', () => {
        // That is a list; borders would add ceremony and no information.
        expect(tables('<table><tr><td>one</td></tr><tr><td>two</td></tr></table>')).toHaveLength(0);
    });
});

describe('parseDocumentBlocks — markdown tables', () => {
    it('recovers a pipe table', () => {
        const [t] = tables(MD_TABLE);
        expect(t.head[0].map((c) => c.text)).toEqual([
            'WHOLE ROCK', 'AND AGE', 'DATE SAMPLE NUMBERS',
        ]);
        expect(t.body).toHaveLength(2);
        expect(t.body[0].map((c) => c.text)).toEqual(['56032', 'WR', '614']);
    });

    it('needs a divider row — a sentence with a pipe is still prose', () => {
        const blocks = parseDocumentBlocks('grade | tonnage was discussed at length');
        expect(blocks.map((b) => b.kind)).toEqual(['text']);
    });

    it('keeps prose that happens to sit between two tables', () => {
        const blocks = parseDocumentBlocks(`${MD_TABLE}\n\nA note between.\n\n${MD_TABLE}`);
        expect(blocks.map((b) => b.kind)).toEqual(['table', 'text', 'table']);
    });
});

describe('empty-column handling', () => {
    it('finds the columns an OCR table left blank in every row', () => {
        // Columns 1 and 2 are empty throughout.
        const [t] = tables(
            '<table><tr><td>a</td><td></td><td></td><td>d</td></tr>' +
                '<tr><td>e</td><td></td><td></td><td>h</td></tr></table>',
        );
        expect([...emptyColumns(t)].sort()).toEqual([1, 2]);
    });

    it('does not drop a column that is populated in even one row', () => {
        const [t] = tables(
            '<table><tr><td>a</td><td></td><td>c</td></tr>' +
                '<tr><td>d</td><td>X</td><td>f</td></tr></table>',
        );
        expect(emptyColumns(t).has(1)).toBe(false);
    });

    it('measures width across the widest row, spans included', () => {
        const [t] = tables(HTML_TABLE);
        expect(tableWidth(t)).toBe(8);
    });
});

describe('parseDocumentBlocks — degenerate input', () => {
    it('returns nothing for an empty body', () => {
        expect(parseDocumentBlocks('')).toEqual([]);
        expect(parseDocumentBlocks('   \n  ')).toEqual([]);
    });

    it('passes plain prose straight through', () => {
        const blocks = parseDocumentBlocks('GENERALIZED GEOLOGIC MAP OF UNGA ISLAND, ALASKA');
        expect(blocks).toHaveLength(1);
        expect(blocks[0].kind).toBe('text');
    });

    it('does not lose text when a table tag never closes', () => {
        const blocks = parseDocumentBlocks('before <table><tr><td>x</td></tr> after');
        expect(blocks.map((b) => b.kind)).toEqual(['text']);
        expect((blocks[0] as { text: string }).text).toContain('after');
    });
});
