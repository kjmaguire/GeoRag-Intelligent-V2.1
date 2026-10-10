/**
 * Shown over a map that could not start (see lib/mapInit). The parent must
 * be positioned; every map container in the app already is.
 */
export default function MapStartFailure({ message }: { message: string }) {
    return (
        <div
            role="alert"
            data-testid="map-start-failure"
            className="absolute inset-0 z-10 flex items-center justify-center p-6 text-center text-sm leading-relaxed"
            style={{ background: 'var(--bg-1, #0b0b0f)', color: 'var(--fg-2, #9ca3af)' }}
        >
            <p className="max-w-md">{message}</p>
        </div>
    );
}
