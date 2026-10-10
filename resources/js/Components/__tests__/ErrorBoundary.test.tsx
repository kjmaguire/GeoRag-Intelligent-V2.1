/**
 * ErrorBoundary crash telemetry. The POST goes to /api/v1/client-errors, a
 * route in the `api` group behind Sanctum's stateful middleware: without the
 * XSRF token it is a 419, the report is never recorded, and (before
 * bootstrap.ts listed the path) the patched window.fetch bounced the page to
 * /login, replacing the "Something went wrong" panel.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from '../ErrorBoundary';

function Boom(): never {
    throw new Error('map exploded');
}

function clearCookies() {
    for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    }
}

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
    clearCookies();
    // React and the boundary both log the caught error; keep the run readable.
    vi.spyOn(console, 'error').mockImplementation(() => {});
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
});

afterEach(() => {
    cleanup();
    clearCookies();
    vi.restoreAllMocks();
});

describe('ErrorBoundary telemetry', () => {
    it('POSTs the crash report with the current XSRF token', () => {
        document.cookie = 'XSRF-TOKEN=tok%3D1; path=/';
        render(
            <ErrorBoundary scope="viz">
                <Boom />
            </ErrorBoundary>,
        );

        expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
        expect(fetchSpy).toHaveBeenCalledTimes(1);
        const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(url).toBe('/api/v1/client-errors');
        expect(init.method).toBe('POST');
        expect(init.credentials).toBe('same-origin');
        const headers = init.headers as Record<string, string>;
        expect(headers['X-XSRF-TOKEN']).toBe('tok=1');
        expect(headers).not.toHaveProperty('X-CSRF-TOKEN');
        expect(headers['Content-Type']).toBe('application/json');
        expect(JSON.parse(String(init.body))).toMatchObject({ scope: 'viz', message: 'map exploded' });
    });

    it('still reports (and still shows the panel) when there is no token to send', () => {
        render(
            <ErrorBoundary>
                <Boom />
            </ErrorBoundary>,
        );

        expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
        const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
        expect(init.headers as Record<string, string>).not.toHaveProperty('X-XSRF-TOKEN');
    });

    it('keeps the panel up when the telemetry request itself fails', async () => {
        fetchSpy.mockRejectedValue(new TypeError('network down'));
        render(
            <ErrorBoundary>
                <Boom />
            </ErrorBoundary>,
        );
        await Promise.resolve();
        expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
    });
});
