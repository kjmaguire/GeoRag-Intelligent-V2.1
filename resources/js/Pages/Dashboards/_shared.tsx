import type { JSX } from 'react';

/**
 * Tiny shared primitives for the §16.1 customer dashboards.
 */

export function StatCard({
    label, value, sub,
}: {
    label: string;
    value: string | number;
    sub?: string;
}): JSX.Element {
    return (
        <div className="rounded-lg border border-zinc-200 bg-white p-4">
            <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
            <div className="mt-1 text-2xl font-semibold text-zinc-900">{value}</div>
            {sub && <div className="mt-1 text-xs text-zinc-500">{sub}</div>}
        </div>
    );
}

export function EmptyState({ message }: { message: string }): JSX.Element {
    return (
        <div className="rounded border border-dashed border-zinc-300 bg-zinc-50 p-6 text-center text-sm text-zinc-500">
            {message}
        </div>
    );
}

export function SectionCard({
    title, children, sub,
}: {
    title: string;
    sub?: string;
    children: React.ReactNode;
}): JSX.Element {
    return (
        <section className="rounded-lg border border-zinc-200 bg-white p-4">
            <h2 className="text-sm font-medium text-zinc-900">{title}</h2>
            {sub && <p className="text-xs text-zinc-500">{sub}</p>}
            <div className="mt-3">{children}</div>
        </section>
    );
}

export function DataTable<T extends Record<string, unknown>>({
    rows, columns,
}: {
    rows: T[];
    columns: { key: keyof T; label: string; render?: (v: unknown, row: T) => React.ReactNode }[];
}): JSX.Element {
    if (rows.length === 0) {
        return <EmptyState message="No rows yet." />;
    }
    return (
        <table className="min-w-full divide-y divide-zinc-200 text-sm">
            <thead className="bg-zinc-50">
                <tr>
                    {columns.map((c) => (
                        <th key={String(c.key)} className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-zinc-500">
                            {c.label}
                        </th>
                    ))}
                </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
                {rows.map((r, i) => (
                    <tr key={i} className="hover:bg-zinc-50">
                        {columns.map((c) => (
                            <td key={String(c.key)} className="px-3 py-2 text-zinc-700">
                                {c.render ? c.render(r[c.key], r) : String(r[c.key] ?? '—')}
                            </td>
                        ))}
                    </tr>
                ))}
            </tbody>
        </table>
    );
}
