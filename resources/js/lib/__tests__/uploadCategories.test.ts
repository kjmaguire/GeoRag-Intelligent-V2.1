import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  CATEGORY_EXTS,
  CATEGORY_LABEL,
  LIVE_CATEGORIES,
  RETIRED_CATEGORIES,
  acceptedExtensions,
  categoryForExtension,
  extensionOf,
  type Category,
} from '../uploadCategories';

/**
 * These tests read UploadController.php directly.
 *
 * That is deliberate. The frontend's idea of which categories exist had
 * drifted from the backend's in BOTH directions and neither was caught:
 *
 *   - the picker offered `spatial` for .kmz/.kml, which the API has never
 *     accepted, so choosing one produced a 422 after the upload
 *   - the import wizard hardcoded PDF/TIFF/ZIP and refused drill and GIS
 *     files client-side, so they stayed unreachable after the API began
 *     accepting them
 *
 * A test that only checked the TypeScript against itself would have passed
 * throughout. Parsing the PHP is the only thing that actually pins them
 * together.
 */

const CONTROLLER = resolve(
  __dirname,
  '../../../../app/Http/Controllers/Api/V1/UploadController.php',
);

function phpCategoryBlock(constName: string): Record<string, string[]> {
  const php = readFileSync(CONTROLLER, 'utf8');
  const block = new RegExp(
    `private const ${constName} = \\[(.*?)\\n    \\];`,
    's',
  ).exec(php);
  if (!block) throw new Error(`${constName} not found in UploadController`);

  const out: Record<string, string[]> = {};
  for (const [, cat, exts] of block[1].matchAll(
    /'([a-z_]+)'\s*=>\s*\[([^\]]*)\]/g,
  )) {
    out[cat] = [...exts.matchAll(/'([a-z0-9]+)'/g)].map((m) => m[1]).sort();
  }
  return out;
}

describe('upload categories match the backend', () => {
  const live = phpCategoryBlock('CATEGORIES');
  const retired = phpCategoryBlock('RETIRED_CATEGORIES');

  it('offers exactly the categories the API accepts', () => {
    expect(LIVE_CATEGORIES.slice().sort()).toEqual(Object.keys(live).sort());
  });

  it('accepts exactly the extensions the API accepts, per category', () => {
    for (const [cat, exts] of Object.entries(live)) {
      expect(
        CATEGORY_EXTS[cat as Category]?.slice().sort(),
        `extensions for '${cat}'`,
      ).toEqual(exts);
    }
  });

  it('never offers a category the API would refuse', () => {
    for (const cat of Object.keys(retired)) {
      expect(
        LIVE_CATEGORIES,
        `'${cat}' is retired in UploadController but offered in the UI`,
      ).not.toContain(cat as Category);
    }
  });

  it('labels every category it can offer', () => {
    for (const cat of LIVE_CATEGORIES) {
      expect(CATEGORY_LABEL[cat], `label for '${cat}'`).toBeTruthy();
    }
  });
});

describe('categoryForExtension', () => {
  it('routes the geology formats to a live category', () => {
    expect(categoryForExtension('pdf')).toBe('reports');
    expect(categoryForExtension('tiff')).toBe('reports');
    expect(categoryForExtension('xlsx')).toBe('excel');
    expect(categoryForExtension('shp')).toBe('spatial');
    expect(categoryForExtension('gpkg')).toBe('spatial');
    expect(categoryForExtension('qgz')).toBe('spatial');
    expect(categoryForExtension('las')).toBe('well_logs');
  });

  it('never returns a retired category', () => {
    // .sgy and .xyz map to retired categories, so they must resolve to null
    // rather than to a category the upload would be refused for.
    expect(categoryForExtension('sgy')).toBeNull();
    expect(categoryForExtension('segy')).toBeNull();
    expect(categoryForExtension('xyz')).toBeNull();

    for (const ext of ['csv', 'pdf', 'shp', 'las', 'zip', 'xlsx']) {
      const cat = categoryForExtension(ext);
      expect(cat, `'${ext}' resolved to a category`).not.toBeNull();
      expect(RETIRED_CATEGORIES.has(cat as Category)).toBe(false);
    }
  });

  it('prefers archive over spatial for a bare .zip', () => {
    // Both accept .zip. A mixed bundle of reports is the commoner upload,
    // and the picker lets the user override to `spatial` for a shapefile.
    expect(categoryForExtension('zip')).toBe('archive');
  });

  it('refuses raster images outright', () => {
    for (const ext of ['jpg', 'png', 'gif', 'bmp']) {
      expect(categoryForExtension(ext), `'${ext}'`).toBeNull();
    }
  });

  it('does not treat TIFF as an unsupported image', () => {
    // TIFF scans route through `reports` and normalise to PDF (ADR-0005).
    expect(categoryForExtension('tif')).toBe('reports');
  });

  it('returns null for something nobody accepts', () => {
    expect(categoryForExtension('exe')).toBeNull();
    expect(categoryForExtension('')).toBeNull();
  });
});

describe('extensionOf', () => {
  it('lowercases and takes the last segment', () => {
    expect(extensionOf('Collars.CSV')).toBe('csv');
    expect(extensionOf('a.b.c.gpkg')).toBe('gpkg');
  });

  it('handles a name with no extension', () => {
    expect(extensionOf('README')).toBe('readme');
  });
});

describe('acceptedExtensions', () => {
  it('lists only extensions of live categories', () => {
    const accepted = acceptedExtensions();
    expect(accepted).toContain('shp');
    expect(accepted).toContain('las');
    expect(accepted).toContain('qgz');
    // .sgy belongs only to a retired category.
    expect(accepted).not.toContain('sgy');
  });
});
