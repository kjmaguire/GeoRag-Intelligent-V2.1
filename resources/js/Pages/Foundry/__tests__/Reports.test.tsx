/**
 * Foundry/Reports — the document list's pager. Paging keeps the open
 * document, the document links carry the page the list is on, and a stale
 * `?page=` past the end falls back to the last page.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

const inertia = vi.hoisted(() => ({
    get: vi.fn(),
    reload: vi.fn(),
    // The Inertia page url (path + query) the page is currently on.
    url: '/projects/red-star/reports',
}));

vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    Link: ({
        href,
        data,
        children,
        only: _only,
        preserveState: _ps,
        preserveScroll: _psc,
        ...rest
    }: {
        href: string;
        data?: Record<string, unknown>;
        children: ReactNode;
        only?: string[];
        preserveState?: boolean;
        preserveScroll?: boolean;
    }) => (
        <a href={href} data-query={data ? JSON.stringify(data) : undefined} {...rest}>
            {children}
        </a>
    ),
    router: { get: inertia.get, reload: inertia.reload, visit: vi.fn() },
    usePage: () => ({ url: inertia.url }),
}));
vi.mock('@/Hooks/useWorkspaceDataUpdated', () => ({ useWorkspaceDataUpdated: () => {} }));
vi.mock('@/Components/Foundry/ReportsViewBar', () => ({ default: () => null }));
vi.mock('@/Components/Foundry/DocumentBody', () => ({ default: () => null }));

import FoundryReports, { type ReportListRow } from '../Reports';

function row(id: string, name: string): ReportListRow {
    return {
        report_id: id,
        title: name,
        source_filename: `${name}.pdf`,
        company: 'Acme',
        filing_date: '2024-01-01',
        commodity: 'U',
        version: 1,
        is_scanned: false,
        parse_quality_pct: 90,
        text_page_coverage_pct: 100,
        sections_count: 3,
        has_content: true,
        passages: 10,
        embedded: 10,
        status: 'ok',
    };
}

function props(over: Record<string, unknown> = {}) {
    return {
        project: { project_id: 'p-1', project_name: 'Red Star', slug: 'red-star' },
        reports: [row('r-1', 'Alpha'), row('r-2', 'Beta')],
        reports_pagination: { total: 120, page: 2, per_page: 50, last_page: 3 },
        quality: {
            totals: { accepted: 5, flagged: 0, rejected: 0, awaiting_ocr: 0 },
            passages_total: 20,
            embedded_total: 20,
            documents: 120,
            documents_not_retrievable: 0,
            pass_gate: true,
        },
        empty: false,
        selected_id: null as string | null,
        report: null,
        sections: [],
        passages: [],
        ...over,
    };
}

beforeEach(() => {
    inertia.get.mockClear();
    inertia.url = '/projects/red-star/reports';
});
afterEach(() => {
    cleanup();
});

describe('Foundry/Reports pager', () => {
    it('renders the pager only when there is more than one page', () => {
        const { unmount } = render(<FoundryReports {...props()} />);
        expect(screen.getByTestId('reports-pager')).toBeInTheDocument();
        expect(screen.getByText('Page 2 of 3')).toBeInTheDocument();
        unmount();

        render(
            <FoundryReports {...props({ reports_pagination: { total: 2, page: 1, per_page: 50, last_page: 1 } })} />,
        );
        expect(screen.queryByTestId('reports-pager')).toBeNull();
    });

    it('goes to the next page and keeps the open document in the URL', () => {
        render(<FoundryReports {...props({ selected_id: 'r-2' })} />);

        fireEvent.click(screen.getByRole('button', { name: 'Next page of documents' }));

        expect(inertia.get).toHaveBeenCalledTimes(1);
        const [url, data, options] = inertia.get.mock.calls[0];
        expect(url).toBe('/projects/red-star/reports/r-2');
        expect(data).toEqual({ page: 3, per_page: 50 });
        expect(options).toMatchObject({
            preserveState: true,
            preserveScroll: true,
            only: ['reports', 'reports_pagination'],
        });
    });

    it('pages without a document open against the bare list URL', () => {
        render(<FoundryReports {...props()} />);

        fireEvent.click(screen.getByRole('button', { name: 'Previous page of documents' }));

        expect(inertia.get.mock.calls[0][0]).toBe('/projects/red-star/reports');
        expect(inertia.get.mock.calls[0][1]).toEqual({ page: 1, per_page: 50 });
    });

    it('makes each document link carry the page and page size', () => {
        render(<FoundryReports {...props({ selected_id: 'r-2' })} />);

        const link = screen.getByRole('link', { name: /Alpha\.pdf/ });
        expect(link).toHaveAttribute('href', '/projects/red-star/reports/r-1');
        expect(JSON.parse(link.getAttribute('data-query') ?? 'null')).toEqual({ page: 2, per_page: 50 });
    });

    it('does not page past the ends', () => {
        render(
            <FoundryReports {...props({ reports_pagination: { total: 120, page: 3, per_page: 50, last_page: 3 } })} />,
        );
        expect(screen.getByRole('button', { name: 'Next page of documents' })).toBeDisabled();
    });

    describe('a stale page beyond the last', () => {
        it('shows the last page and asks the server for it, replacing the history entry', () => {
            render(
                <FoundryReports
                    {...props({
                        selected_id: 'r-2',
                        reports: [],
                        reports_pagination: { total: 120, page: 9, per_page: 50, last_page: 3 },
                    })}
                />,
            );

            expect(screen.getByText('Page 3 of 3')).toBeInTheDocument();
            expect(screen.getByText('101–120 of 120 documents')).toBeInTheDocument();
            expect(screen.getByRole('button', { name: 'Next page of documents' })).toBeDisabled();

            expect(inertia.get).toHaveBeenCalledTimes(1);
            const [url, data, options] = inertia.get.mock.calls[0];
            expect(url).toBe('/projects/red-star/reports/r-2');
            expect(data).toEqual({ page: 3, per_page: 50 });
            expect(options).toMatchObject({ replace: true });
        });

        it('Prev from the clamped page goes to the page before the last, not before the stale one', () => {
            render(
                <FoundryReports
                    {...props({ reports_pagination: { total: 120, page: 9, per_page: 50, last_page: 3 } })}
                />,
            );
            inertia.get.mockClear();

            fireEvent.click(screen.getByRole('button', { name: 'Previous page of documents' }));

            expect(inertia.get.mock.calls[0][1]).toEqual({ page: 2, per_page: 50 });
        });

        it('does not re-request anything when the page is in range', () => {
            render(<FoundryReports {...props()} />);
            expect(inertia.get).not.toHaveBeenCalled();
        });
    });
});

describe('Foundry/Reports ?section= deep link (FE-9)', () => {
    // jsdom has no scrollIntoView; the highlighted section scrolls itself into view.
    const scrollIntoView = vi.fn();
    beforeEach(() => {
        scrollIntoView.mockClear();
        Element.prototype.scrollIntoView = scrollIntoView;
    });
    afterEach(() => {
        window.history.replaceState({}, '', '/');
    });

    /** An Inertia visit: the page url and the browser's address bar move together. */
    function visit(url: string) {
        inertia.url = url;
        window.history.replaceState({}, '', url);
    }

    function detail(id: string) {
        return {
            report_id: id,
            title: `Report ${id}`,
            company: 'Acme',
            filing_date: '2024-01-01',
            commodity: 'U',
            version: 1,
            region: 'WY',
            project_name: 'Red Star',
            parse_quality_pct: 0.9,
            text_page_coverage_pct: 1,
            is_scanned: false,
            page_count: 10,
            parser_used: 'fitz',
            parser_label: null,
            created_at: '2024-01-01',
            updated_at: '2024-01-01',
            has_source: false,
        };
    }
    const sections = ['1', '2', '3'].map((heading, index) => ({
        heading,
        body: `body ${heading}`,
        kind: 'para',
        index,
    }));

    /** Props for report `id` open in the reader, with sections "1", "2" and "3". */
    function open(id: string) {
        return props({
            selected_id: id,
            report: detail(id),
            sections,
            reports_pagination: { total: 2, page: 1, per_page: 50, last_page: 1 },
        });
    }
    const outlined = (container: HTMLElement) =>
        Array.from(container.querySelectorAll<HTMLElement>('[style*="outline"]')).map((el) => el.textContent ?? '');

    it('outlines the section a citation deep-links to, and scrolls to it', () => {
        visit('/projects/red-star/reports/r-1?section=2');
        const { container } = render(<FoundryReports {...open('r-1')} />);

        const hits = outlined(container);
        expect(hits).toHaveLength(1);
        expect(hits[0]).toContain('SECTION 2');
        expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });

    it('does not carry the outline over to the next report the reader picks', () => {
        visit('/projects/red-star/reports/r-1?section=2');
        const { container, rerender } = render(<FoundryReports {...open('r-1')} />);
        expect(outlined(container)).toHaveLength(1);

        // Picking another document in the list is an Inertia visit to its own url:
        // same component instance, no ?section=.
        visit('/projects/red-star/reports/r-2?page=1&per_page=50');
        rerender(<FoundryReports {...open('r-2')} />);

        expect(outlined(container)).toEqual([]);
    });

    it('follows the url to another section when it changes under the same page', () => {
        visit('/projects/red-star/reports/r-1?section=2');
        const { container, rerender } = render(<FoundryReports {...open('r-1')} />);

        visit('/projects/red-star/reports/r-2?section=3');
        rerender(<FoundryReports {...open('r-2')} />);

        const hits = outlined(container);
        expect(hits).toHaveLength(1);
        expect(hits[0]).toContain('SECTION 3');
        // ...and scrolls to the new target.
        expect(scrollIntoView).toHaveBeenCalledTimes(2);
    });

    it('outlines nothing for a plain link or an empty ?section=', () => {
        visit('/projects/red-star/reports/r-1');
        const { container, rerender } = render(<FoundryReports {...open('r-1')} />);
        expect(outlined(container)).toEqual([]);

        visit('/projects/red-star/reports/r-1?section=');
        rerender(<FoundryReports {...open('r-1')} />);
        expect(outlined(container)).toEqual([]);
        expect(scrollIntoView).not.toHaveBeenCalled();
    });
});
