import { Head, Link } from '@inertiajs/react';
import AppLayout from '@/Layouts/AppLayout';
import { PageHeader, Card, Pill, EmptyState } from '@/Components/Foundry/primitives';
import ReportsViewBar from '@/Components/Foundry/ReportsViewBar';
import { formatWhen } from '@/lib/time';

/**
 * Foundry Attribute Tables — the read path for `silver.attribute_tables`.
 *
 * That table holds the rows that match no geology schema: standalone dBASE
 * (`.dbf` / MapInfo `.dat`) tables, and any delimited sheet the drill
 * classifier could not type. A dBASE table's columns are whatever the person
 * who made it typed, so the rows land whole as JSONB and the column set has
 * to be computed rather than declared — see AttributeTablesController.
 *
 * Until this page existed the table had no reader at all, so a delivery that
 * wrote hundreds of rows (measured: 229 from one RedStar project, 854 from
 * one soil survey) was invisible to the geologist who asked for those files
 * by name.
 *
 * Master-detail: the tables in this project on the left, the selected
 * table's rows on the right. Selection and paging are query-string state
 * (`?table={sha256}&layer={name}&page=N`) reloaded as Inertia partials, so
 * clicking a table never re-runs the list query.
 */

/** One cell after the controller has flattened it — never an object. */
type Cell = string | number | boolean | null;

/** One entry in the master list: a distinct (file, layer) pair. */
interface AttributeTableSummary {
    /** The uploaded file's name. Null on rows landed before it was recorded. */
    source_file: string | null;
    /** dBASE layer name; for a bare `.dbf` this is the file stem. */
    source_layer: string;
    /** Half of the table's identity — the other half is source_layer. */
    source_file_sha256: string;
    rows: number;
    updated_at: string | null;
}

/**
 * A derived column. `numeric` is decided server-side across a sample of rows
 * rather than per-cell in the browser: one blank in an assay column would
 * otherwise flip that cell to left-aligned and break the decimal alignment
 * for the whole column.
 */
interface DerivedColumn {
    name: string;
    numeric: boolean;
}

interface AttributeTableDetail {
    source_file: string | null;
    source_file_sha256: string;
    source_layer: string;
    columns: DerivedColumn[];
    /** How many rows the header was derived from. Bounded, and shown. */
    sampled_rows: number;
    total_rows: number;
    page: number;
    per_page: number;
    last_page: number;
    rows: Array<{ row_index: number; cells: Cell[] }>;
    /** Keys on THIS page that the header sample never saw. Usually empty. */
    extra_columns_on_page: string[];
}

interface AttributeTablesProps {
    project: { project_id: string; project_name: string; slug: string };
    tables: AttributeTableSummary[];
    selected: { source_file_sha256: string; source_layer: string } | null;
    table: AttributeTableDetail | null;
}

/**
 * Props the detail pane owns. Clicking a table in the list asks for only
 * these, so the master list and its GROUP BY survive the visit untouched.
 */
const DETAIL_PROPS = ['selected', 'table'] as const;

function tableHref(slug: string, t: { source_file_sha256: string; source_layer: string }, page?: number): string {
    const params = new URLSearchParams({
        table: t.source_file_sha256,
        layer: t.source_layer,
    });
    if (page && page > 1) params.set('page', String(page));

    return `/projects/${slug}/attribute-tables?${params.toString()}`;
}

/** The label a geologist recognises: the filename, falling back to the layer. */
function tableLabel(t: { source_file: string | null; source_layer: string }): string {
    return t.source_file ?? t.source_layer;
}

export default function FoundryAttributeTables({
    project,
    tables,
    selected,
    table,
}: AttributeTablesProps) {
    const empty = tables.length === 0;
    const totalRows = tables.reduce((sum, t) => sum + t.rows, 0);

    return (
        <AppLayout>
            <Head
                title={
                    table
                        ? `${tableLabel(table)} · ${project.project_name}`
                        : `Attribute tables · ${project.project_name}`
                }
            />

            {/* overflow-hidden on the page shell, min-w-0 on the detail
                column: the row grid can be 111 columns wide and must scroll
                inside its own container. Without min-w-0 a flex child
                refuses to shrink below its content and the whole page body
                scrolls sideways instead. */}
            <div
                className="flex-1 flex flex-col overflow-hidden"
                style={{ background: 'var(--bg-0)', color: 'var(--fg-1)' }}
            >
                <PageHeader
                    eyebrow={`PROJECT · ${project.project_name.toUpperCase()} · REPORTS · TABLES`}
                    title="Attribute tables"
                    sub={
                        empty
                            ? 'Nothing landed yet'
                            : `${tables.length} table${tables.length === 1 ? '' : 's'} · ` +
                              `${totalRows.toLocaleString()} row${totalRows === 1 ? '' : 's'}`
                    }
                />

                {/* The same View row Reports renders — this page is the
                    tabular half of the same delivery, not a separate place. */}
                <div
                    className="flex items-center gap-3 px-8 py-2 border-b shrink-0"
                    style={{ background: 'var(--bg-1)', borderColor: 'var(--line-1)' }}
                >
                    <span
                        className="text-[10px] font-mono uppercase tracking-widest"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        View
                    </span>
                    <ReportsViewBar slug={project.slug} active="tables" />
                </div>

                {empty ? (
                    <div className="px-8 py-12">
                        <EmptyState
                            title="No attribute tables in this project."
                            detail="Standalone .dbf / .dat tables and sheets that match no drill schema land in silver.attribute_tables — rows kept whole, with the file they came from. Upload one through the Import Wizard and it will appear here."
                            action={
                                <Link
                                    href="/foundry/imports/wizard"
                                    className="inline-block text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                                    style={{
                                        color: 'var(--accent)',
                                        background: 'var(--accent-bg)',
                                        borderColor: 'var(--accent-dim)',
                                    }}
                                >
                                    ↑ Upload files →
                                </Link>
                            }
                        />
                    </div>
                ) : (
                    <div className="flex-1 flex min-h-0">
                        <TableList
                            tables={tables}
                            slug={project.slug}
                            selected={selected}
                        />

                        <section className="flex-1 min-w-0 flex flex-col">
                            {table ? (
                                <TablePane slug={project.slug} table={table} />
                            ) : (
                                <div className="px-8 py-12">
                                    <EmptyState
                                        title="Pick a table."
                                        detail="Each entry on the left is one file-and-layer pair. Its columns are computed from the rows themselves — these tables carry no declared schema."
                                    />
                                </div>
                            )}
                        </section>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}

/* ------------------------------------------------------------------ */
/* Master list                                                          */
/* ------------------------------------------------------------------ */

function TableList({
    tables,
    slug,
    selected,
}: {
    tables: AttributeTableSummary[];
    slug: string;
    selected: { source_file_sha256: string; source_layer: string } | null;
}) {
    return (
        <nav
            className="w-[320px] shrink-0 overflow-y-auto border-r"
            style={{ borderColor: 'var(--line-1)' }}
            aria-label="Attribute tables"
        >
            <div
                className="px-4 py-2 text-[10px] font-mono uppercase tracking-wider border-b sticky top-0"
                style={{
                    color: 'var(--fg-3)',
                    borderColor: 'var(--line-1)',
                    background: 'var(--bg-0)',
                }}
            >
                {tables.length} table{tables.length === 1 ? '' : 's'}
            </div>

            {tables.map((t) => {
                const active =
                    selected !== null &&
                    selected.source_file_sha256 === t.source_file_sha256 &&
                    selected.source_layer === t.source_layer;

                return (
                    <Link
                        key={`${t.source_file_sha256}:${t.source_layer}`}
                        href={tableHref(slug, t)}
                        // Only the detail pane changes. preserveScroll stops a
                        // long list jumping to the top on every click.
                        only={[...DETAIL_PROPS]}
                        preserveState
                        preserveScroll
                        aria-current={active ? 'true' : undefined}
                        className="block px-4 py-3 border-b"
                        style={{
                            borderColor: 'var(--line-1)',
                            background: active ? 'var(--accent-bg)' : 'transparent',
                            borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                        }}
                    >
                        <div
                            className="text-[12px] leading-snug truncate"
                            style={{ color: active ? 'var(--accent)' : 'var(--fg-0)' }}
                            title={tableLabel(t)}
                        >
                            {tableLabel(t)}
                        </div>
                        {/* The layer is kept, demoted, when it says something
                            the filename does not — a multi-sheet workbook
                            lands one entry per sheet and the sheet name is
                            the only thing telling them apart. */}
                        {t.source_file && t.source_layer !== t.source_file && (
                            <div
                                className="text-[11px] leading-snug truncate"
                                style={{ color: 'var(--fg-2)' }}
                                title={t.source_layer}
                            >
                                {t.source_layer}
                            </div>
                        )}
                        <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
                            <Pill tone="neutral">
                                {t.rows.toLocaleString()} row{t.rows === 1 ? '' : 's'}
                            </Pill>
                            <span
                                className="text-[10px] font-mono"
                                style={{ color: 'var(--fg-3)' }}
                            >
                                {formatWhen(t.updated_at)}
                            </span>
                        </div>
                    </Link>
                );
            })}
        </nav>
    );
}

/* ------------------------------------------------------------------ */
/* Detail pane                                                          */
/* ------------------------------------------------------------------ */

function TablePane({ slug, table }: { slug: string; table: AttributeTableDetail }) {
    const firstRow = (table.page - 1) * table.per_page + 1;
    const lastRow = Math.min(table.page * table.per_page, table.total_rows);

    return (
        <div className="flex-1 min-w-0 flex flex-col overflow-hidden px-8 py-5">
            <Card
                className="flex-1 min-h-0 flex flex-col"
                contentClassName="flex-1 min-h-0 flex flex-col"
                padded={false}
                eyebrow={`LAYER · ${table.source_layer.toUpperCase()}`}
                title={tableLabel(table)}
                actions={
                    <span
                        className="text-[10px] font-mono tabular-nums"
                        style={{ color: 'var(--fg-3)' }}
                    >
                        {table.total_rows.toLocaleString()} rows ·{' '}
                        {table.columns.length} columns
                    </span>
                }
            >
                <ColumnProvenance table={table} />

                {table.columns.length === 0 ? (
                    <EmptyState
                        title="This table's rows carry no columns."
                        detail="Every sampled row is an empty JSON object. The file parsed, but nothing came out of it — worth opening the source to check."
                    />
                ) : (
                    <>
                        {/* The ONLY horizontally scrolling element on the
                            page. 111 columns is normal for a soil survey, so
                            this container scrolls and the page body does
                            not. */}
                        <div className="flex-1 min-h-0 overflow-auto">
                            <RowGrid table={table} />
                        </div>
                        <Pager slug={slug} table={table} firstRow={firstRow} lastRow={lastRow} />
                    </>
                )}
            </Card>
        </div>
    );
}

/**
 * Says where the header came from.
 *
 * The column set is derived from a bounded head sample, so it CAN miss a key
 * that first appears deeper in the file. Stating the sample size, and naming
 * any key the current page has that the header does not, is the difference
 * between a caveat and a silently missing column.
 */
function ColumnProvenance({ table }: { table: AttributeTableDetail }) {
    const sampled = table.sampled_rows < table.total_rows;
    const extra = table.extra_columns_on_page;

    if (!sampled && extra.length === 0) return null;

    return (
        <div
            className="px-4 py-2 border-b text-[10px] font-mono"
            style={{
                borderColor: 'var(--line-1)',
                color: extra.length > 0 ? 'var(--warn)' : 'var(--fg-3)',
            }}
        >
            {sampled && (
                <span>
                    Columns derived from the first {table.sampled_rows} of{' '}
                    {table.total_rows.toLocaleString()} rows.
                </span>
            )}
            {extra.length > 0 && (
                <span>
                    {' '}
                    This page also carries {extra.length} column
                    {extra.length === 1 ? '' : 's'} the sample missed:{' '}
                    {extra.join(', ')}.
                </span>
            )}
        </div>
    );
}

function RowGrid({ table }: { table: AttributeTableDetail }) {
    return (
        <table className="text-[11px] border-collapse" style={{ minWidth: '100%' }}>
            <thead>
                <tr>
                    {/* Sticky in BOTH axes. At 111 columns the row number is
                        the only thing telling you where you are once you
                        have scrolled right, and the header is the only thing
                        telling you what a column is once you have scrolled
                        down. Both need an opaque background — a transparent
                        sticky cell shows the rows sliding under it. */}
                    <th
                        className="sticky top-0 left-0 z-20 px-3 py-2 text-right font-mono uppercase tracking-wider border-b border-r"
                        style={{
                            color: 'var(--fg-3)',
                            background: 'var(--bg-1)',
                            borderColor: 'var(--line-1)',
                        }}
                        scope="col"
                    >
                        #
                    </th>
                    {table.columns.map((c) => (
                        <th
                            key={c.name}
                            scope="col"
                            title={c.name}
                            className={[
                                'sticky top-0 z-10 px-3 py-2 font-mono uppercase tracking-wider border-b whitespace-nowrap',
                                c.numeric ? 'text-right' : 'text-left',
                            ].join(' ')}
                            style={{
                                color: 'var(--fg-3)',
                                background: 'var(--bg-1)',
                                borderColor: 'var(--line-1)',
                            }}
                        >
                            {c.name}
                        </th>
                    ))}
                </tr>
            </thead>
            <tbody>
                {table.rows.map((row) => (
                    <tr key={row.row_index}>
                        <td
                            className="sticky left-0 z-10 px-3 py-1.5 text-right font-mono tabular-nums border-b border-r"
                            style={{
                                color: 'var(--fg-3)',
                                background: 'var(--bg-1)',
                                borderColor: 'var(--line-1)',
                            }}
                        >
                            {row.row_index}
                        </td>
                        {row.cells.map((cell, i) => (
                            <DataCell
                                key={table.columns[i].name}
                                value={cell}
                                numeric={table.columns[i].numeric}
                            />
                        ))}
                    </tr>
                ))}
            </tbody>
        </table>
    );
}

function DataCell({ value, numeric }: { value: Cell; numeric: boolean }) {
    // Null and empty string are both "no value here" to a reader, and both
    // arrive from real files — a dBASE NULL and a blank CSV cell. Rendering
    // "" leaves a hole that reads as a layout bug; the em dash reads as
    // absence.
    const blank = value === null || value === '';
    const text = blank ? '—' : String(value);

    return (
        <td
            className={[
                'px-3 py-1.5 border-b whitespace-nowrap',
                numeric ? 'text-right tabular-nums font-mono' : 'text-left',
            ].join(' ')}
            style={{
                color: blank ? 'var(--fg-3)' : 'var(--fg-1)',
                borderColor: 'var(--line-1)',
            }}
            title={blank ? undefined : text}
        >
            {text}
        </td>
    );
}

function Pager({
    slug,
    table,
    firstRow,
    lastRow,
}: {
    slug: string;
    table: AttributeTableDetail;
    firstRow: number;
    lastRow: number;
}) {
    const hasPrev = table.page > 1;
    const hasNext = table.page < table.last_page;

    return (
        <div
            className="flex items-center justify-between px-4 py-2 border-t shrink-0"
            style={{ borderColor: 'var(--line-1)' }}
        >
            <span
                className="text-[10px] font-mono tabular-nums"
                style={{ color: 'var(--fg-3)' }}
            >
                Rows {firstRow.toLocaleString()}–{lastRow.toLocaleString()} of{' '}
                {table.total_rows.toLocaleString()}
            </span>

            <div className="flex items-center gap-2">
                <PagerLink
                    slug={slug}
                    table={table}
                    page={table.page - 1}
                    enabled={hasPrev}
                    label="← Prev"
                />
                <span
                    className="text-[10px] font-mono tabular-nums"
                    style={{ color: 'var(--fg-2)' }}
                >
                    {table.page} / {table.last_page}
                </span>
                <PagerLink
                    slug={slug}
                    table={table}
                    page={table.page + 1}
                    enabled={hasNext}
                    label="Next →"
                />
            </div>
        </div>
    );
}

function PagerLink({
    slug,
    table,
    page,
    enabled,
    label,
}: {
    slug: string;
    table: AttributeTableDetail;
    page: number;
    enabled: boolean;
    label: string;
}) {
    const className =
        'text-[10px] font-mono uppercase tracking-wider px-2.5 py-1 rounded border';

    // A disabled <Link> is still a link: it navigates on click and reads as
    // actionable to a screen reader. At the ends of the range this has to be
    // a real disabled control, not a dimmed anchor.
    if (!enabled) {
        return (
            <button
                type="button"
                disabled
                className={`${className} opacity-40`}
                style={{ color: 'var(--fg-3)', borderColor: 'var(--line-2)' }}
            >
                {label}
            </button>
        );
    }

    return (
        <Link
            href={tableHref(slug, table, page)}
            only={[...DETAIL_PROPS]}
            preserveState
            preserveScroll
            className={className}
            style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}
        >
            {label}
        </Link>
    );
}
