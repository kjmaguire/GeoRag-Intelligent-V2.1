import { useCallback, useEffect, useState } from 'react';

/**
 * Toggleable "fullscreen-within-app" state with Esc-to-exit.
 *
 * This is NOT the browser Fullscreen API — that hides the URL bar and
 * extension chrome, which is jarring inside an Inertia SPA. Instead we
 * model it as a parent-level boolean that consumers map onto Tailwind
 * classes (e.g. `fixed inset-0 z-[100]` + `hidden` on sidebars) so the
 * map can take over the visible app area without leaving the SPA.
 *
 * Esc handler is only attached while the toggle is on so the listener
 * doesn't fight other components when the page isn't fullscreen.
 *
 * Returned tuple matches useState's shape so callers can destructure as
 * `const [isFullscreen, setIsFullscreen] = useFullscreenToggle()`.
 */
export function useFullscreenToggle(initial = false) {
    const [isFullscreen, setIsFullscreen] = useState<boolean>(initial);

    useEffect(() => {
        if (!isFullscreen) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setIsFullscreen(false);
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [isFullscreen]);

    const toggle = useCallback(() => setIsFullscreen((v) => !v), []);

    return { isFullscreen, setIsFullscreen, toggle };
}
