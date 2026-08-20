import { describe, expect, it } from 'vitest';
import JSZip from 'jszip';
import { groupShapefiles, isShapefileSidecar } from '@/lib/shapefileBundle';

/**
 * The bug these pin: a folder import queued `geology_poly.shp` on its own and
 * dropped its `.shx`/`.dbf`/`.prj` as "unsupported", because only `.shp` has an
 * upload category. Ingestion then failed with GDAL's
 * "Unable to open geology_poly.shx" for every shapefile in the project.
 */

function makeFile(name: string, dir = '', body = 'x'): File {
    const f = new File([body], name.split('/').pop() as string);
    if (dir) {
        Object.defineProperty(f, 'webkitRelativePath', {
            value: `${dir}/${f.name}`,
        });
    }
    return f;
}

async function membersOf(file: File): Promise<string[]> {
    const zip = await JSZip.loadAsync(await file.arrayBuffer());
    return Object.keys(zip.files).sort();
}

describe('groupShapefiles', () => {
    it('zips a shapefile with its sidecars instead of uploading the .shp alone', async () => {
        const { bundles, passthrough, orphanSidecars } = await groupShapefiles([
            makeFile('geology_poly.shp'),
            makeFile('geology_poly.shx'),
            makeFile('geology_poly.dbf'),
            makeFile('geology_poly.prj'),
        ]);

        expect(bundles).toHaveLength(1);
        expect(passthrough).toHaveLength(0);
        expect(orphanSidecars).toHaveLength(0);
        expect(bundles[0].file.name).toBe('geology_poly.zip');
        expect(await membersOf(bundles[0].file)).toEqual([
            'geology_poly.dbf',
            'geology_poly.prj',
            'geology_poly.shp',
            'geology_poly.shx',
        ]);
        expect(bundles[0].missing).toEqual([]);
    });

    it('reports which required sidecars were absent without refusing the upload', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('faults.shp'),
            makeFile('faults.dbf'),
        ]);

        // GDAL can often rebuild a missing index, and a missing .prj only costs
        // the declared CRS — so this is a note, not a rejection.
        expect(bundles).toHaveLength(1);
        expect(bundles[0].missing).toEqual(['shx', 'prj']);
    });

    it('keeps same-named shapefiles in different folders apart', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('faults.shp', 'geology'),
            makeFile('faults.shx', 'geology'),
            makeFile('faults.shp', 'claims'),
            makeFile('faults.shx', 'claims'),
        ]);

        expect(bundles).toHaveLength(2);
        for (const b of bundles) {
            expect(await membersOf(b.file)).toEqual(['faults.shp', 'faults.shx']);
        }
    });

    it('leaves non-shapefile uploads alone', async () => {
        const files = [
            makeFile('report.pdf'),
            makeFile('collars.csv'),
            makeFile('map.tif'),
        ];
        const { bundles, passthrough } = await groupShapefiles(files);

        expect(bundles).toHaveLength(0);
        expect(passthrough.map((f) => f.name)).toEqual([
            'report.pdf',
            'collars.csv',
            'map.tif',
        ]);
    });

    it('treats a sidecar with no .shp beside it as unusable, not as a passthrough upload', async () => {
        const { bundles, passthrough, orphanSidecars } = await groupShapefiles([
            makeFile('orphan.dbf'),
        ]);

        expect(bundles).toHaveLength(0);
        expect(passthrough).toHaveLength(0);
        expect(orphanSidecars.map((f) => f.name)).toEqual(['orphan.dbf']);
    });

    it('does not absorb a same-named file that is not a sidecar', async () => {
        // faults.pdf is its own report, not part of the shapefile.
        const { bundles, passthrough } = await groupShapefiles([
            makeFile('faults.shp'),
            makeFile('faults.shx'),
            makeFile('faults.pdf'),
        ]);

        expect(await membersOf(bundles[0].file)).toEqual([
            'faults.shp',
            'faults.shx',
        ]);
        expect(passthrough.map((f) => f.name)).toEqual(['faults.pdf']);
    });

    it('bundles the .cpg so non-ASCII attribute values are not mangled', async () => {
        const { bundles } = await groupShapefiles([
            makeFile('claims.shp'),
            makeFile('claims.shx'),
            makeFile('claims.dbf'),
            makeFile('claims.prj'),
            makeFile('claims.cpg'),
        ]);

        expect(await membersOf(bundles[0].file)).toContain('claims.cpg');
    });

    it('matches sidecar extensions case-insensitively', async () => {
        const { bundles, orphanSidecars } = await groupShapefiles([
            makeFile('UPPER.SHP'),
            makeFile('UPPER.SHX'),
        ]);

        expect(orphanSidecars).toHaveLength(0);
        expect(bundles).toHaveLength(1);
        expect(await membersOf(bundles[0].file)).toEqual(['UPPER.SHP', 'UPPER.SHX']);
    });
});

describe('isShapefileSidecar', () => {
    it.each(['shx', 'dbf', 'prj', 'cpg', 'SHX'])('recognises .%s', (ext) => {
        expect(isShapefileSidecar(ext)).toBe(true);
    });

    it.each(['shp', 'pdf', 'csv', 'zip'])('does not claim .%s', (ext) => {
        expect(isShapefileSidecar(ext)).toBe(false);
    });
});
