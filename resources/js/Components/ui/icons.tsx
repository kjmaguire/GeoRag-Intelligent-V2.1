import * as React from 'react';

/**
 * Minimal hand-rolled icon set for `Components/ui/` primitives.
 *
 * `lucide-react` is not a dependency (see package.json) — the rest of the
 * app already hand-rolls inline SVGs rather than pulling in an icon
 * library (e.g. the thread-rail hamburger button in
 * `Pages/Foundry/Chat.tsx`), so this follows the same convention instead
 * of adding one just for `sheet.tsx`'s close button.
 */
export function X(props: React.SVGProps<SVGSVGElement>) {
    return (
        <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            {...props}
        >
            <path d="M18 6 6 18M6 6l12 12" />
        </svg>
    );
}
