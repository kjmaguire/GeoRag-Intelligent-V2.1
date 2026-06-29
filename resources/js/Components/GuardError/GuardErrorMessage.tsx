/**
 * Plan §4d — GuardErrorMessage primitive.
 *
 * Resolves a `GuardErrorCode` + placeholders against the i18n catalog
 * shared via Inertia (`page.props.guard_errors`). Handles the two
 * degradation variants that mirror the Laravel-side
 * `App\Services\Guards\GuardErrorRenderer`:
 *
 *   ENTITY_NOT_FOUND → ENTITY_NOT_FOUND_NO_ALIASES
 *     when `suggested_aliases` is empty / missing
 *
 *   CONFLICTING_SOURCES → CONFLICTING_SOURCES_WITH_AUTHORITY
 *     when an `authoritative_doc` placeholder is provided
 *
 * Behaviour matches the server-side renderer 1:1 so an answer can be
 * rendered either side (server-formatted via `__()` OR client-side via
 * this primitive). The two paths produce identical text.
 *
 * Specific UI surfaces (banner, picker, conflict side-by-side, partial
 * answer card, incident report) compose this primitive — see the
 * sibling files in `Components/GuardError/`. This file is the
 * foundation; it does NOT render any visual chrome.
 */

import type { JSX } from "react";
import { usePage } from "@inertiajs/react";

/**
 * The full canonical set of plan §4b GuardErrorCode values plus the
 * two degradation variants the renderer dispatches to. Mirrors
 * `App\Services\Guards\GuardErrorRenderer::KNOWN_CODES` and the
 * Python `app.agent.guards.GuardErrorCode` enum.
 */
export type GuardErrorCode =
    // Retrieval-failure codes (9)
    | "NO_EVIDENCE_FOUND"
    | "ENTITY_NOT_FOUND"
    | "AMBIGUOUS_HOLE_ID"
    | "AMBIGUOUS_FORMATION_NAME"
    | "AMBIGUOUS_PROPERTY_NAME"
    | "OVER_FILTERED_QUERY"
    | "SPATIAL_QUERY_EMPTY"
    | "SPATIAL_CRS_MISMATCH"
    | "GRAPH_PATH_NOT_FOUND"
    // Evidence-quality codes (6)
    | "NUMERIC_GROUNDING_FAILED"
    | "CITATION_INCOMPLETE"
    | "CONFLICTING_SOURCES"
    | "MISSING_DEPTH_INTERVAL"
    | "MISSING_ASSAY_UNITS"
    | "SOURCE_SCOPE_VIOLATION"
    // Query-failure code (1)
    | "UNSUPPORTED_QUERY_TYPE"
    // Out-of-band
    | "DEATH_LOOP";

/** All 16 plan-§4b codes plus DEATH_LOOP. Useful for type-narrowing. */
export const ALL_GUARD_ERROR_CODES: readonly GuardErrorCode[] = [
    "NO_EVIDENCE_FOUND",
    "ENTITY_NOT_FOUND",
    "AMBIGUOUS_HOLE_ID",
    "AMBIGUOUS_FORMATION_NAME",
    "AMBIGUOUS_PROPERTY_NAME",
    "OVER_FILTERED_QUERY",
    "SPATIAL_QUERY_EMPTY",
    "SPATIAL_CRS_MISMATCH",
    "GRAPH_PATH_NOT_FOUND",
    "NUMERIC_GROUNDING_FAILED",
    "CITATION_INCOMPLETE",
    "CONFLICTING_SOURCES",
    "MISSING_DEPTH_INTERVAL",
    "MISSING_ASSAY_UNITS",
    "SOURCE_SCOPE_VIOLATION",
    "UNSUPPORTED_QUERY_TYPE",
    "DEATH_LOOP",
];

export type GuardPlaceholderValue = string | number | null | undefined;

export type GuardPlaceholders = Record<string, GuardPlaceholderValue>;

/**
 * Inertia shared-props shape. The `HandleInertiaRequests::share`
 * middleware now includes `guard_errors` on every response (lazy
 * closure). Other shared props are reserved by the rest of the app.
 */
type PageProps = {
    guard_errors?: Record<string, string>;
};

/**
 * Apply the canonical degradation rule. Mirrors the Laravel-side
 * `GuardErrorRenderer::dispatchVariant`.
 */
function effectiveCode(
    code: string,
    placeholders: GuardPlaceholders,
): string {
    if (code === "ENTITY_NOT_FOUND") {
        const aliases = placeholders["suggested_aliases"];
        if (
            aliases === undefined ||
            aliases === null ||
            aliases === ""
        ) {
            return "ENTITY_NOT_FOUND_NO_ALIASES";
        }
    }
    if (code === "CONFLICTING_SOURCES") {
        const authoritative = placeholders["authoritative_doc"];
        if (authoritative !== undefined && authoritative !== null && authoritative !== "") {
            return "CONFLICTING_SOURCES_WITH_AUTHORITY";
        }
    }
    return code;
}

/**
 * Substitute `:placeholder` markers in a template string. Mirrors
 * Laravel's `__()` placeholder replacement semantics for parity with
 * the server-side renderer.
 */
function substitute(
    template: string,
    placeholders: GuardPlaceholders,
): string {
    return template.replace(/:([a-zA-Z_]\w*)/g, (match, name: string) => {
        const value = placeholders[name];
        if (value === undefined || value === null) {
            return match; // leave the literal `:name` (Laravel default)
        }
        return String(value);
    });
}

/**
 * Pure resolution function — no React. Exposed for testing + reuse in
 * non-component callers (event handlers, transformers, snapshot
 * builders).
 */
export function resolveGuardErrorMessage(
    code: string,
    placeholders: GuardPlaceholders,
    catalog: Record<string, string> | undefined,
): string {
    const effective = effectiveCode(code, placeholders);
    if (!catalog) {
        return effective; // fallback when Inertia share hasn't populated yet
    }
    const template = catalog[effective];
    if (template === undefined) {
        const fallback = catalog["UNSUPPORTED_QUERY_TYPE"];
        if (fallback !== undefined) {
            return substitute(fallback, {
                reason: `internal: unknown guard code '${code}'`,
                specific_alternative_action: "rephrase your question",
            });
        }
        return effective;
    }
    return substitute(template, placeholders);
}

export interface GuardErrorMessageProps {
    code: string;
    placeholders?: GuardPlaceholders;
    /**
     * Override the catalog source. Tests pass an inline catalog;
     * production reads from Inertia shared props.
     */
    catalog?: Record<string, string>;
}

/**
 * Renders a guard error message as plain text. NO visual chrome —
 * specific surface components (RefusalBanner, AmbiguityPicker, etc.)
 * wrap this and add their own framing.
 *
 * Usage:
 *   <GuardErrorMessage
 *     code="ENTITY_NOT_FOUND"
 *     placeholders={{ entity: "Rowan", suggested_aliases: "WRLG Rowan" }}
 *   />
 */
export function GuardErrorMessage({
    code,
    placeholders = {},
    catalog,
}: GuardErrorMessageProps): JSX.Element {
    const shared = usePage<PageProps>().props.guard_errors;
    const resolved = resolveGuardErrorMessage(code, placeholders, catalog ?? shared);
    return <span data-guard-code={code}>{resolved}</span>;
}

export default GuardErrorMessage;
