/**
 * Foundry/DataImportWizard — FE-2: the server's upload ceiling is enforced
 * before any bytes are sent, and a 413 reads as the limit.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

const inertia = vi.hoisted(() => ({ props: {} as Record<string, unknown>, visit: vi.fn() }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    usePage: () => ({ props: inertia.props, url: '/import' }),
    router: { visit: inertia.visit },
    Link: ({ href, children }: { href: string; children: ReactNode }) => <a href={href}>{children}</a>,
}));

import DataImportWizard from '../DataImportWizard';

let uploads: string[];
let uploadHeaders: Array<Record<string, string>>;
let uploadStatus: number;

beforeEach(() => {
    uploads = [];
    uploadHeaders = [];
    uploadStatus = 200;
    inertia.props = { upload_limit: { bytes: 10_000, human: '10 KB' } };
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === '/api/v1/projects') {
            return new Response(
                JSON.stringify({ data: [{ project_id: 'p-1', slug: 'red-star', project_name: 'Red Star' }] }),
                { status: 200 },
            );
        }
        uploads.push(((init?.body as FormData).get('file') as File).name);
        uploadHeaders.push((init?.headers ?? {}) as Record<string, string>);
        return uploadStatus === 200
            ? new Response('{}', { status: 200 })
            : new Response('<html>Request Entity Too Large</html>', { status: uploadStatus });
    });
});
afterEach(() => {
    vi.restoreAllMocks();
    inertia.visit.mockReset();
});

async function addFiles(files: File[]) {
    const view = render(<DataImportWizard />);
    await screen.findByRole('option', { name: /Red Star/ });
    const input = view.container.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
        fireEvent.change(input, { target: { files } });
    });
    return view;
}

describe('DataImportWizard upload limit (FE-2)', () => {
    it('flags and refuses an over-limit file without uploading it', async () => {
        await addFiles([
            new File([new Uint8Array(20_000)], 'huge.pdf', { type: 'application/pdf' }),
            new File(['%PDF-1.4'], 'small.pdf', { type: 'application/pdf' }),
        ]);
        expect(await screen.findByText(/over the 10 KB limit/)).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /start ingest/i }));
        await screen.findByText(/Exceeds the 10 KB upload limit\./);
        expect(uploads).toEqual(['small.pdf']);
        // Partial failure: stay on the page.
        expect(inertia.visit).not.toHaveBeenCalled();
    });

    it('sends the live XSRF cookie token on each upload, never the stale csrf meta tag', async () => {
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        document.cookie = 'XSRF-TOKEN=live-token; path=/';
        try {
            await addFiles([new File(['%PDF-1.4'], 'small.pdf', { type: 'application/pdf' })]);
            fireEvent.click(await screen.findByRole('button', { name: /start ingest/i }));
            await waitFor(() => expect(uploads).toEqual(['small.pdf']));

            expect(uploadHeaders[0]['X-XSRF-TOKEN']).toBe('live-token');
            expect(uploadHeaders[0]).not.toHaveProperty('X-CSRF-TOKEN');
        } finally {
            document.head.innerHTML = '';
            document.cookie = 'XSRF-TOKEN=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
        }
    });

    it('explains a 413 as the limit rather than "HTTP 413"', async () => {
        uploadStatus = 413;
        await addFiles([new File(['%PDF-1.4'], 'small.pdf', { type: 'application/pdf' })]);
        fireEvent.click(await screen.findByRole('button', { name: /start ingest/i }));
        await waitFor(() => expect(screen.getByText(/Exceeds the 10 KB upload limit\./)).toBeInTheDocument());
        expect(screen.queryByText('HTTP 413')).toBeNull();
    });
});
