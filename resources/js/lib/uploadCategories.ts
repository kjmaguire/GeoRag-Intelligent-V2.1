/**
 * Upload categories, mirroring App\Http\Controllers\Api\V1\UploadController.
 *
 * This exists because the same knowledge was duplicated in two places and
 * drifted in both directions:
 *
 *   - NewProject.tsx offered `spatial` for .kmz/.kml, which the backend has
 *     never accepted, and offered `seismic`/`xyz`, which it refuses — so the
 *     picker promised uploads that came back 422.
 *   - DataImportWizard.tsx accepted only PDF/TIFF/ZIP and refused everything
 *     else client-side, so the drill and GIS formats were unreachable from
 *     that screen even after the API started accepting them.
 *
 * One module, imported by both. When UploadController::CATEGORIES changes,
 * this is the single file that changes with it.
 */

export type Category =
  | 'reports'
  | 'archive'
  | 'collars'
  | 'surveys'
  | 'lithology'
  | 'samples'
  | 'excel'
  | 'spatial'
  | 'well_logs'
  | 'seismic'
  | 'xyz';

/** Extensions each category accepts. Mirrors UploadController::CATEGORIES. */
export const CATEGORY_EXTS: Record<Category, string[]> = {
  reports: ['pdf', 'tif', 'tiff'],
  archive: ['zip'],
  collars: ['csv', 'txt', 'tsv'],
  surveys: ['csv', 'txt', 'tsv'],
  lithology: ['csv', 'txt', 'tsv'],
  samples: ['csv', 'txt', 'tsv'],
  excel: ['xlsx', 'xls', 'xlsm'],
  // ZIP is here because a shapefile is never one file — .shp/.shx/.dbf/.prj
  // travel together and a lone .shp cannot be read without them.
  spatial: [
    'geojson', 'json', 'shp', 'gpkg', 'gml', 'gpx', 'dxf', 'dgn',
    'fgb', 'gdb', 'zip', 'qgs', 'qgz',
  ],
  well_logs: ['las'],
  seismic: ['sgy', 'segy'],
  xyz: ['xyz', 'dat', 'txt'],
};

export const CATEGORY_LABEL: Record<Category, string> = {
  reports: 'NI 43-101 / reports (PDF, TIFF)',
  archive: 'Archive of mixed files (ZIP)',
  collars: 'Drill collars (CSV)',
  surveys: 'Down-hole surveys (CSV)',
  lithology: 'Lithology logs (CSV)',
  samples: 'Assay samples (CSV)',
  excel: 'Excel workbooks (XLSX)',
  spatial: 'Spatial / GIS (SHP, GeoPackage, GeoJSON, QGIS, ZIP)',
  well_logs: 'Well logs (LAS)',
  seismic: 'Seismic (SEG-Y)',
  xyz: 'XYZ grids / point data',
};

/**
 * Categories the backend still refuses with 422
 * (UploadController::RETIRED_CATEGORIES).
 *
 * Offering one means the user picks a category, waits through the upload and
 * is then told no. Filter these out of every picker, and move an entry out of
 * here the moment its workflow ships.
 */
export const RETIRED_CATEGORIES = new Set<Category>(['seismic', 'xyz']);

/** Categories a user may actually choose. */
export const LIVE_CATEGORIES = (Object.keys(CATEGORY_LABEL) as Category[]).filter(
  (c) => !RETIRED_CATEGORIES.has(c),
);

/** Image formats the backend rejects outright. TIFF is NOT one of them — it
 *  routes through `reports` and is normalised to PDF (ADR-0005). */
export const UNSUPPORTED_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'bmp']);

export function extensionOf(filename: string): string {
  return filename.split('.').pop()?.toLowerCase() ?? '';
}

/**
 * Best category for a file extension, or null when nothing accepts it.
 *
 * Order matters where an extension is ambiguous. `.zip` resolves to
 * `archive` (a mixed bundle of reports) rather than `spatial`, because that
 * is the commoner case and the user can override in the picker. `.csv`
 * resolves to `collars` for the same reason — it is the file people upload
 * first, and the other drill types are one click away.
 */
export function categoryForExtension(ext: string): Category | null {
  if (UNSUPPORTED_EXTS.has(ext)) return null;

  const preference: Category[] = [
    'reports',
    'archive',
    'collars',
    'surveys',
    'lithology',
    'samples',
    'excel',
    'spatial',
    'well_logs',
  ];

  for (const cat of preference) {
    if (RETIRED_CATEGORIES.has(cat)) continue;
    if (CATEGORY_EXTS[cat].includes(ext)) return cat;
  }
  return null;
}

/** Every extension any live category accepts — for an `accept=` attribute. */
export function acceptedExtensions(): string[] {
  const all = new Set<string>();
  for (const cat of LIVE_CATEGORIES) {
    for (const ext of CATEGORY_EXTS[cat]) all.add(ext);
  }
  return [...all].sort();
}
