/**
 * Written against a real failure: a geologist dropped five folders holding 72
 * files across 44 sub-folders onto the import wizard and nothing uploaded. The
 * wizard read `dataTransfer.files`, which for a dropped folder contains a
 * single 0-byte File named after the directory, and reported five
 * "unsupported files".
 */
import { describe, expect, it } from 'vitest';

import { filesFromDataTransfer, looksLikeFolder } from '../dropFiles';

/* ------------------------------------------------------------------ *
 * Fakes for the drag-drop entry API, which jsdom does not implement.
 * ------------------------------------------------------------------ */

interface Tree {
    [name: string]: Tree | string;
}

function fileEntry(name: string, content: string) {
    return {
        isFile: true,
        isDirectory: false,
        name,
        file: (ok: (f: File) => void) => ok(new File([content], name)),
    };
}

/**
 * A directory entry whose reader hands back at most `batchSize` children per
 * call, mirroring the real `readEntries` contract.
 */
function dirEntry(name: string, tree: Tree, batchSize = 100) {
    const children = Object.entries(tree).map(([childName, value]) =>
        typeof value === 'string' ? fileEntry(childName, value) : dirEntry(childName, value, batchSize),
    );
    return {
        isFile: false,
        isDirectory: true,
        name,
        createReader() {
            let cursor = 0;
            return {
                readEntries(ok: (e: unknown[]) => void) {
                    const batch = children.slice(cursor, cursor + batchSize);
                    cursor += batch.length;
                    ok(batch);
                },
            };
        },
    };
}

function dataTransferOf(entries: unknown[], files: File[] = []): DataTransfer {
    return {
        items: entries.map((entry) => ({ webkitGetAsEntry: () => entry })),
        files,
    } as unknown as DataTransfer;
}

const relPath = (f: File) => (f as File & { webkitRelativePath?: string }).webkitRelativePath;

describe('filesFromDataTransfer', () => {
    it('pulls every file out of a deeply nested folder', async () => {
        // Six levels, matching Centennial/Geophysics/IP/June 19/L3750N/export/.
        const tree: Tree = {
            Geophysics: { IP: { 'June 19': { L3750N: { export: { 'IP.inp': 'x', 'dcinv2d.011': 'y' } } } } },
        };

        const files = await filesFromDataTransfer(dataTransferOf([dirEntry('Centennial', tree)]));

        expect(files.map((f) => f.name).sort()).toEqual(['IP.inp', 'dcinv2d.011']);
    });

    it('does not stop at the first 100 children', async () => {
        // readEntries returns at most 100 per call and signals completion with
        // an empty batch. A single call silently truncates a large folder —
        // which on a real delivery is indistinguishable from data loss.
        const many: Tree = {};
        for (let i = 0; i < 250; i++) many[`f${i}.csv`] = 'x';

        const files = await filesFromDataTransfer(dataTransferOf([dirEntry('big', many)]));

        expect(files).toHaveLength(250);
    });

    it('stamps webkitRelativePath so the bundler can still tell folders apart', async () => {
        // groupKey() in shapefileBundle keys a dataset on relative path + stem.
        // The entry API leaves webkitRelativePath empty, so without stamping,
        // two different faults.shp collapse into one scrambled bundle.
        const tree: Tree = {
            geology: { 'faults.shp': 'a' },
            claims: { 'faults.shp': 'b' },
        };

        const files = await filesFromDataTransfer(dataTransferOf([dirEntry('delivery', tree)]));

        expect(files.map(relPath).sort()).toEqual([
            'delivery/claims/faults.shp',
            'delivery/geology/faults.shp',
        ]);
    });

    it('leaves a path the directory picker already set alone', async () => {
        // A `webkitdirectory` pick carries a real path. Overwriting it with a
        // reconstructed one is how the two intake routes would drift apart.
        const picked = new File(['x'], 'a.csv');
        Object.defineProperty(picked, 'webkitRelativePath', { value: 'real/path/a.csv' });
        const entry = {
            isFile: true,
            isDirectory: false,
            name: 'a.csv',
            file: (ok: (f: File) => void) => ok(picked),
        };

        const files = await filesFromDataTransfer(dataTransferOf([entry]));

        expect(relPath(files[0])).toBe('real/path/a.csv');
    });

    it('collects every dropped item, not just the first', async () => {
        const files = await filesFromDataTransfer(
            dataTransferOf([
                dirEntry('A', { 'a.csv': 'x' }),
                dirEntry('B', { 'b.csv': 'x' }),
                dirEntry('C', { 'c.csv': 'x' }),
            ]),
        );

        expect(files.map((f) => f.name).sort()).toEqual(['a.csv', 'b.csv', 'c.csv']);
    });

    it('falls back to the plain file list when the entry API is absent', async () => {
        const plain = new File(['x'], 'loose.csv');
        const dt = { items: undefined, files: [plain] } as unknown as DataTransfer;

        expect((await filesFromDataTransfer(dt)).map((f) => f.name)).toEqual(['loose.csv']);
    });

    it('falls back when an entry yields nothing rather than returning empty', async () => {
        // A folder the browser refuses to read must not silently swallow the
        // plain list that came with the same drop.
        const plain = new File(['x'], 'loose.csv');
        const unreadable = { isFile: false, isDirectory: true, name: 'nope', createReader: () => ({ readEntries: (ok: (e: unknown[]) => void) => ok([]) }) };

        const files = await filesFromDataTransfer(dataTransferOf([unreadable], [plain]));

        expect(files.map((f) => f.name)).toEqual(['loose.csv']);
    });

    it('drops a file the browser cannot open instead of queueing a phantom', async () => {
        const broken = {
            isFile: true,
            isDirectory: false,
            name: 'locked.csv',
            file: (_ok: (f: File) => void, err: () => void) => err(),
        };

        expect(await filesFromDataTransfer(dataTransferOf([broken]))).toEqual([]);
    });

    it('returns nothing for a null dataTransfer', async () => {
        expect(await filesFromDataTransfer(null)).toEqual([]);
    });
});

describe('looksLikeFolder', () => {
    it('recognises the 0-byte typeless File a dropped folder produces', () => {
        expect(looksLikeFolder(new File([], 'Apollo Sitka'))).toBe(true);
    });

    it('does not mistake a real file for one', () => {
        expect(looksLikeFolder(new File(['data'], 'a.csv', { type: 'text/csv' }))).toBe(false);
    });
});
