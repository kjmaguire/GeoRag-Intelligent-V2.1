import '@testing-library/jest-dom/vitest';

// Polyfill ResizeObserver — used by Radix UI Popover and other floating-element
// primitives that jsdom doesn't implement. Tests that open Radix popovers/menus
// will fail with "ResizeObserver is not defined" without this stub.
if (typeof globalThis.ResizeObserver === 'undefined') {
    globalThis.ResizeObserver = class ResizeObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
}

// Polyfill window.matchMedia — used by some media-query-aware components.
if (typeof window !== 'undefined' && typeof window.matchMedia === 'undefined') {
    Object.defineProperty(window, 'matchMedia', {
        writable: true,
        value: (query: string) => ({
            matches: false,
            media: query,
            onchange: null,
            addListener: () => {},
            removeListener: () => {},
            addEventListener: () => {},
            removeEventListener: () => {},
            dispatchEvent: () => false,
        }),
    });
}
