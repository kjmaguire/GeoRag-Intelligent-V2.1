/**
 * Escape a value for safe interpolation into an HTML string.
 *
 * Audit 2026-06-27 (T6): MapLibre popups build raw HTML from DB feature
 * properties and hand it to `Popup.setHTML()`, which does NOT sanitize. Ingested
 * values (hole_id, survey_name, sample_id, …) can contain markup, so every
 * value interpolated into popup HTML MUST pass through this helper to prevent
 * stored XSS in the map UI.
 *
 * Order matters: escape `&` first so the entity ampersands added afterwards are
 * not double-escaped.
 */
export function escapeHtml(value: unknown): string {
    const s = value == null ? '' : String(value);

    return s
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
