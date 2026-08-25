import { router } from '@inertiajs/react';
import { Segmented } from './primitives';

/**
 * The Reports view selector, shared by Reports and Attribute Tables.
 *
 * Both pages read the same thing from two angles: a delivery's files, split
 * into the prose half (`silver.reports` + `silver.document_passages`) and the
 * tabular half (`silver.attribute_tables`). A `.dbf` beside a shapefile, a
 * MapInfo `.dat`, an Access table and a spreadsheet whose sheets matched no
 * drill layout all land in the second — same upload, same provenance, same
 * source file — so a nav entry of its own two rows away was the wrong shape.
 *
 * Tables keeps its own URL rather than becoming a panel inside Reports, for
 * the reason spelled out in WorkspaceModeBar: the catalogue has its own
 * controller, its own RLS-scoped query and a feature suite pinning
 * `/projects/{slug}/attribute-tables` to `Foundry/AttributeTables`. One
 * renderer, two URLs, one bar.
 */
export type ReportsViewId = 'documents' | 'tables';

const VIEWS: ReadonlyArray<{ value: ReportsViewId; label: string; suffix: string }> = [
    { value: 'documents', label: 'Documents', suffix: '/reports' },
    { value: 'tables', label: 'Tables', suffix: '/attribute-tables' },
];

export default function ReportsViewBar({
    slug,
    active,
}: {
    slug: string;
    active: ReportsViewId;
}) {
    return (
        <Segmented<ReportsViewId>
            value={active}
            onChange={(next) => {
                if (next === active) return;
                const target = VIEWS.find((v) => v.value === next);
                if (target) router.visit(`/projects/${slug}${target.suffix}`);
            }}
            options={VIEWS.map((v) => ({ value: v.value, label: v.label }))}
        />
    );
}
