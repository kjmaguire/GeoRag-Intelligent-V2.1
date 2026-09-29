/**
 * The server's upload ceiling, on the client (FE-2, 2026-09-29).
 *
 * NewProject hard-coded 6 GiB ("matches UploadController + Octane limits"),
 * the import wizard checked nothing, and the server's single ceiling is
 * App\Support\Uploads::maxBytes() — 512 MiB by default, which is also the
 * Swoole packet cap. A 900 MB GeoTIFF passed the client, uploaded for
 * minutes, and died as a dropped connection ("Failed to fetch") or a bare
 * "HTTP 413". The server now shares its value as the `upload_limit` Inertia
 * prop (HandleInertiaRequests::share) and both screens enforce it before
 * sending a byte.
 */
import { usePage } from '@inertiajs/react';

export interface UploadLimit {
    bytes: number;
    /** Server-formatted, e.g. "512 MB". */
    human: string;
}

/** Mirrors Uploads::DEFAULT_MAX_BYTES; only used if the prop is missing. */
export const DEFAULT_UPLOAD_LIMIT: UploadLimit = { bytes: 512 * 1024 * 1024, human: '512 MB' };

export function uploadLimitFromProps(props: Record<string, unknown> | undefined): UploadLimit {
    const raw = props?.upload_limit as Partial<UploadLimit> | undefined;
    if (raw && typeof raw.bytes === 'number' && raw.bytes > 0) {
        return { bytes: raw.bytes, human: typeof raw.human === 'string' && raw.human ? raw.human : DEFAULT_UPLOAD_LIMIT.human };
    }
    return DEFAULT_UPLOAD_LIMIT;
}

export function useUploadLimit(): UploadLimit {
    const page = usePage<{ upload_limit?: UploadLimit }>();
    return uploadLimitFromProps(page.props as Record<string, unknown>);
}

export function exceedsUploadLimit(size: number, limit: UploadLimit): boolean {
    return size > limit.bytes;
}

export function tooLargeMessage(limit: UploadLimit): string {
    return `Exceeds the ${limit.human} upload limit.`;
}

/**
 * A readable reason for a failed upload.
 *
 * @param status  HTTP status, or null when fetch itself threw (the connection
 *                was dropped — what Swoole does to an over-cap body).
 * @param serverMessage  the JSON `message`, when the server sent one.
 */
export function describeUploadFailure(
    status: number | null,
    serverMessage: string | undefined,
    fileSize: number,
    limit: UploadLimit,
): string {
    if (status === 413) return tooLargeMessage(limit);
    if (status === null) {
        // A dropped connection on a file at or near the cap is the transport
        // refusing the body, not the network.
        return fileSize >= limit.bytes * 0.95
            ? tooLargeMessage(limit)
            : 'Network error — the upload did not reach the server. Check the connection and retry.';
    }
    if (serverMessage) return serverMessage;
    return `Upload failed (HTTP ${status}).`;
}
