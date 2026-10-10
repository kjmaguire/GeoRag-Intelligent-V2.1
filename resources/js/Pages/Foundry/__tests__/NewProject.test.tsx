/**
 * Foundry/NewProject — FE-1 (upload failures are shown, not navigated away
 * from; retry re-uploads only the failures into the SAME project) and FE-2
 * (the server's upload ceiling is enforced before upload).
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

const page = vi.hoisted(() => ({ props: {} as Record<string, unknown> }));
vi.mock('@inertiajs/react', () => ({
    Head: () => null,
    usePage: () => ({ props: page.props, url: '/projects/new' }),
    router: { visit: vi.fn() },
    Link: ({ href, children }: { href: string; children: ReactNode }) => <a href={href}>{children}</a>,
}));

import NewProject from '../NewProject';

type Call = { url: string; body: unknown; headers: Record<string, string> };

let calls: Call[];
let uploadResponses: Array<() => Response>;
// Queued answers for POST /api/v1/projects; empty means the usual 201.
let createResponses: Array<() => Response>;
let hrefSets: string[];
const realLocation = window.location;

beforeEach(() => {
    calls = [];
    createResponses = [];
    hrefSets = [];
    page.props = { upload_limit: { bytes: 10_000, human: '10 KB' } };
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, body: init?.body, headers: (init?.headers ?? {}) as Record<string, string> });
        if (url === '/api/v1/projects') {
            const queued = createResponses.shift();
            if (queued) return queued();
            return new Response(JSON.stringify({ data: { project_id: 'p-1', slug: 'red-star' } }), { status: 201 });
        }
        const next = uploadResponses.shift();
        return next ? next() : new Response('{}', { status: 200 });
    });
    // Capture the hard navigation instead of performing it.
    Object.defineProperty(window, 'location', {
        configurable: true,
        value: {
            ...realLocation,
            origin: 'http://localhost',
            set href(v: string) {
                hrefSets.push(v);
            },
            get href() {
                return 'http://localhost/projects/new';
            },
        },
    });
});

afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
});

async function queueFilesAndReview(files: File[]) {
    const view = render(<NewProject />);
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'Red Star' } });
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    const input = view.container.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
        fireEvent.change(input, { target: { files } });
    });
    await screen.findByText(/Queued · /);
    return view;
}

const uploads = () => calls.filter((c) => c.url.endsWith('/upload'));
const projectCreates = () => calls.filter((c) => c.url === '/api/v1/projects');

describe('NewProject', () => {
    it('stays on the page, lists failed uploads, and retries only those into the same project (FE-1)', async () => {
        uploadResponses = [
            () => new Response(JSON.stringify({ message: 'The collar file has no hole id column.' }), { status: 422 }),
            () => new Response('{}', { status: 200 }),
        ];
        await queueFilesAndReview([
            new File(['hole,x,y'], 'collars.csv', { type: 'text/csv' }),
            new File(['%PDF-1.4'], 'report.pdf', { type: 'application/pdf' }),
        ]);
        fireEvent.click(screen.getByRole('button', { name: /next/i }));
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));

        const failed = await screen.findByTestId('failed-uploads');
        expect(failed).toHaveTextContent('collars.csv — The collar file has no hole id column.');
        expect(screen.getByRole('alert')).toHaveTextContent(/1 of 2 files failed/);
        expect(hrefSets).toEqual([]);
        expect(uploads()).toHaveLength(2);

        uploadResponses = [() => new Response('{}', { status: 200 })];
        fireEvent.click(screen.getByRole('button', { name: /retry 1 failed upload/i }));

        await waitFor(() => expect(hrefSets).toEqual(['/projects/red-star/ingestion-runs']));
        expect(projectCreates()).toHaveLength(1);
        expect(uploads()).toHaveLength(3);
        expect(((uploads()[2].body as FormData).get('file') as File).name).toBe('collars.csv');
    });

    it('navigates straight on when every upload succeeds', async () => {
        uploadResponses = [];
        await queueFilesAndReview([new File(['%PDF-1.4'], 'report.pdf', { type: 'application/pdf' })]);
        fireEvent.click(screen.getByRole('button', { name: /next/i }));
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));
        await waitFor(() => expect(hrefSets).toEqual(['/projects/red-star/ingestion-runs']));
    });

    it('refuses a file over the server upload limit before uploading it (FE-2)', async () => {
        uploadResponses = [];
        await queueFilesAndReview([
            new File([new Uint8Array(20_000)], 'huge.pdf', { type: 'application/pdf' }),
            new File(['%PDF-1.4'], 'report.pdf', { type: 'application/pdf' }),
        ]);
        expect(screen.getByText(/over 10 KB/)).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: /next/i }));
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));
        await waitFor(() => expect(hrefSets).toHaveLength(1));
        expect(uploads().map((c) => ((c.body as FormData).get('file') as File).name)).toEqual(['report.pdf']);
    });

    it('sends the live XSRF cookie token on the create and on every upload, never the stale csrf meta tag', async () => {
        // The <meta> token is rendered once at page load and is dead after an
        // SPA sign-out/sign-in; Laravel prefers X-CSRF-TOKEN over the cookie.
        document.head.innerHTML = '<meta name="csrf-token" content="stale-from-page-load">';
        document.cookie = 'XSRF-TOKEN=live-token; path=/';
        try {
            uploadResponses = [];
            await queueFilesAndReview([new File(['%PDF-1.4'], 'report.pdf', { type: 'application/pdf' })]);
            fireEvent.click(screen.getByRole('button', { name: /next/i }));
            fireEvent.click(screen.getByRole('button', { name: /create project/i }));
            await waitFor(() => expect(hrefSets).toHaveLength(1));

            for (const call of [projectCreates()[0], uploads()[0]]) {
                expect(call.headers['X-XSRF-TOKEN']).toBe('live-token');
                expect(call.headers).not.toHaveProperty('X-CSRF-TOKEN');
            }
        } finally {
            document.head.innerHTML = '';
            document.cookie = 'XSRF-TOKEN=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
        }
    });

    it('describes the upload flow with the server limit, not the retired Dagster/bronze wording', () => {
        render(<NewProject />);
        fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'Red Star' } });
        fireEvent.click(screen.getByRole('button', { name: /next/i }));
        fireEvent.click(screen.getByRole('button', { name: /next/i }));

        const note = screen.getByTestId('corpus-upload-note');
        expect(note).toHaveTextContent(
            'Files upload when the project is created and ingest automatically. Per-file limit: 10 KB.',
        );
        expect(note.textContent).not.toMatch(/dagster|bronze|6\s*GB/i);
    });

    it('explains a 413 as the upload limit', async () => {
        uploadResponses = [() => new Response('<html>413</html>', { status: 413 })];
        await queueFilesAndReview([new File(['%PDF-1.4'], 'report.pdf', { type: 'application/pdf' })]);
        fireEvent.click(screen.getByRole('button', { name: /next/i }));
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));
        expect(await screen.findByTestId('failed-uploads')).toHaveTextContent('Exceeds the 10 KB upload limit.');
    });

    describe('azimuth reference (Kyle, 2026-09-29)', () => {
        function toJurisdiction() {
            render(<NewProject />);
            fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'Red Star' } });
            fireEvent.click(screen.getByRole('button', { name: /next/i }));
        }
        function toReview() {
            fireEvent.click(screen.getByRole('button', { name: /next/i }));
            fireEvent.click(screen.getByRole('button', { name: /next/i }));
        }
        const createBody = () => JSON.parse(String(projectCreates()[0].body));

        it('sends BOH, and no declination, when nothing is chosen', async () => {
            toJurisdiction();
            expect(screen.getByLabelText(/Azimuth reference/)).toHaveValue('BOH');
            toReview();
            fireEvent.click(screen.getByRole('button', { name: /create project/i }));
            await waitFor(() => expect(projectCreates()).toHaveLength(1));
            expect(createBody().orientation_reference).toBe('BOH');
            expect(createBody()).not.toHaveProperty('magnetic_declination');
        });

        it('sends magnetic north with its declination as a number, east positive', async () => {
            toJurisdiction();
            fireEvent.change(screen.getByLabelText(/Azimuth reference/), { target: { value: 'magnetic' } });
            fireEvent.change(screen.getByLabelText(/Magnetic declination/), { target: { value: '14.5' } });
            toReview();
            fireEvent.click(screen.getByRole('button', { name: /create project/i }));
            await waitFor(() => expect(projectCreates()).toHaveLength(1));
            expect(createBody().orientation_reference).toBe('magnetic');
            expect(createBody().magnetic_declination).toBe(14.5);
        });

        it('blocks creating a magnetic-north project with no declination', () => {
            toJurisdiction();
            fireEvent.change(screen.getByLabelText(/Azimuth reference/), { target: { value: 'magnetic' } });
            expect(
                screen.getByText('Magnetic north needs a declination (degrees, east positive).'),
            ).toBeInTheDocument();
            toReview();
            expect(screen.getByRole('button', { name: /create project/i })).toBeDisabled();
            expect(projectCreates()).toHaveLength(0);
        });

        it('sends true north with no declination', async () => {
            toJurisdiction();
            fireEvent.change(screen.getByLabelText(/Azimuth reference/), { target: { value: 'true' } });
            expect(screen.queryByLabelText(/Magnetic declination/)).not.toBeInTheDocument();
            toReview();
            fireEvent.click(screen.getByRole('button', { name: /create project/i }));
            await waitFor(() => expect(projectCreates()).toHaveLength(1));
            expect(createBody().orientation_reference).toBe('true');
        });
    });
});

describe('NewProject — project code (FE-10)', () => {
    const createBody = () => JSON.parse(String(projectCreates()[0].body));

    /** Name (and optionally a code) on the first step, then forward to Review. */
    function toReview(code?: string) {
        render(<NewProject />);
        fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'Red Star' } });
        if (code !== undefined) fireEvent.change(screen.getByLabelText('Project code'), { target: { value: code } });
        for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: /next/i }));
    }

    it('sends the code, trimmed, in the create body', async () => {
        toReview('  RS-01 ');
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));

        await waitFor(() => expect(projectCreates()).toHaveLength(1));
        expect(createBody().project_code).toBe('RS-01');
        expect(createBody().project_name).toBe('Red Star');
    });

    it.each([[''], ['   ']])('omits project_code when the field is blank (%j)', async (blank) => {
        toReview(blank);
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));

        await waitFor(() => expect(projectCreates()).toHaveLength(1));
        expect(createBody()).not.toHaveProperty('project_code');
    });

    it('shows the 422 on the code field, takes the user back to it, and uploads nothing', async () => {
        createResponses = [
            () =>
                new Response(
                    JSON.stringify({
                        message: 'This project code is already used in your workspace.',
                        errors: { project_code: ['This project code is already used in your workspace.'] },
                    }),
                    { status: 422 },
                ),
        ];
        toReview('RS-01');
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));

        // Back on the step that owns the field, with the server's words under it.
        // (The message sits inside the field's <label>, so match the name loosely.)
        const field = await screen.findByLabelText(/Project code/);
        await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'));
        expect(screen.getByText('This project code is already used in your workspace.')).toBeInTheDocument();
        expect(screen.getByText(/Step 1 of 4: Identity/)).toBeInTheDocument();
        // Not also reported as a generic banner, and nothing was uploaded.
        expect(screen.getAllByRole('alert')).toHaveLength(1);
        expect(uploads()).toHaveLength(0);
        expect(hrefSets).toEqual([]);

        // Editing the code withdraws the message; creating again then works.
        fireEvent.change(field, { target: { value: 'RS-02' } });
        expect(screen.queryByText('This project code is already used in your workspace.')).toBeNull();
        expect(field).not.toHaveAttribute('aria-invalid');
        for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole('button', { name: /next/i }));
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));
        await waitFor(() => expect(projectCreates()).toHaveLength(2));
        expect(JSON.parse(String(projectCreates()[1].body)).project_code).toBe('RS-02');
    });

    it('keeps other 422s as the usual message under the form', async () => {
        createResponses = [
            () =>
                new Response(
                    JSON.stringify({
                        message: 'The project name has already been taken.',
                        errors: { project_name: ['The project name has already been taken.'] },
                    }),
                    { status: 422 },
                ),
        ];
        toReview('RS-01');
        fireEvent.click(screen.getByRole('button', { name: /create project/i }));

        expect(await screen.findByRole('alert')).toHaveTextContent('The project name has already been taken.');
        expect(screen.getByText(/Step 4 of 4: Review/)).toBeInTheDocument();
    });
});
