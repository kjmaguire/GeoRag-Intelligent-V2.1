/**
 * AmbiguityPicker — plan §4d UI surface for ambiguous-entity cases.
 *
 * Used for: AMBIGUOUS_HOLE_ID, AMBIGUOUS_FORMATION_NAME,
 * AMBIGUOUS_PROPERTY_NAME.
 *
 * Shows the disambiguation prompt + a list of candidate chips. Each
 * candidate is a clickable button — `onPick(candidate)` fires when a
 * geologist disambiguates, and the parent re-issues the query with the
 * picked candidate substituted.
 */

import type { JSX } from "react";
import type { GuardPlaceholders } from "./GuardErrorMessage";
import { GuardErrorMessage } from "./GuardErrorMessage";

export interface AmbiguityPickerProps {
    code: string;
    /**
     * The ambiguous term itself (e.g. "ECK-22-001"). Becomes the
     * `:term` placeholder in the message.
     */
    term: string;
    /**
     * Candidate strings to render as clickable chips. The
     * disambiguation message's `:candidates` placeholder is set to a
     * joined string of these so the prose makes sense too.
     */
    candidates: string[];
    onPick?: (candidate: string) => void;
    placeholders?: GuardPlaceholders;
}

export function AmbiguityPicker({
    code,
    term,
    candidates,
    onPick,
    placeholders = {},
}: AmbiguityPickerProps): JSX.Element {
    const mergedPlaceholders: GuardPlaceholders = {
        ...placeholders,
        term,
        candidates: candidates.join(", "),
    };

    return (
        <div
            data-guard-surface="ambiguity"
            className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
        >
            <p>
                <GuardErrorMessage
                    code={code}
                    placeholders={mergedPlaceholders}
                />
            </p>
            {candidates.length > 0 && (
                <ul
                    role="list"
                    className="mt-2 flex flex-wrap gap-2"
                >
                    {candidates.map((c) => (
                        <li key={c}>
                            <button
                                type="button"
                                onClick={() => onPick?.(c)}
                                className="rounded border border-amber-300 bg-white px-2 py-1 text-xs text-amber-900 hover:border-amber-500 hover:bg-amber-100"
                                data-candidate={c}
                            >
                                {c}
                            </button>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
}

export default AmbiguityPicker;
