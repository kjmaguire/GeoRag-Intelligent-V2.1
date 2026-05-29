/**
 * DepthPickerCard — plan §4b/§4d UI surface for
 * REQUEST_DEPTH_CLARIFICATION terminal strategy.
 *
 * Fires when the agent can't tell whether a depth interval the
 * geologist mentioned is in metres or feet (legacy reports vary,
 * imperial-vs-metric ambiguity is a real cause of incorrect
 * intervals). Each candidate unit is a clickable chip; onPick
 * fires when the geologist picks one, and the parent re-issues
 * the query with the unit fixed.
 *
 * ADR-0009 Stage 2 dependency — see UnitPickerCard.tsx for the
 * sibling component handling assay-unit ambiguity.
 */

import type { GuardPlaceholders } from "./GuardErrorMessage";
import { GuardErrorMessage } from "./GuardErrorMessage";

export interface DepthPickerCardProps {
    code?: string;
    /**
     * The numeric depth value as it appeared in the query
     * (e.g. "250.5"). Becomes the `:value` placeholder.
     */
    value?: string;
    /**
     * Candidate depth units. Defaults to the two practical
     * choices in mining: metres and feet. The disambiguation
     * message's `:candidates` placeholder is set to a joined
     * string of these.
     */
    candidates?: string[];
    onPick?: (candidate: string) => void;
    placeholders?: GuardPlaceholders;
}

const DEFAULT_DEPTH_CANDIDATES: string[] = [
    "m",      // metres — SI / modern standard
    "ft",     // feet — legacy / imperial reports
];

export function DepthPickerCard({
    code = "REQUEST_DEPTH_CLARIFICATION",
    value = "",
    candidates = DEFAULT_DEPTH_CANDIDATES,
    onPick,
    placeholders = {},
}: DepthPickerCardProps): JSX.Element {
    const mergedPlaceholders: GuardPlaceholders = {
        ...placeholders,
        value,
        candidates: candidates.join(", "),
    };

    return (
        <div
            data-guard-surface="depth-picker"
            className="rounded-md border border-teal-200 bg-teal-50 px-4 py-3 text-sm text-teal-900"
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
                    aria-label="Depth unit candidates"
                >
                    {candidates.map((c) => (
                        <li key={c}>
                            <button
                                type="button"
                                onClick={() => onPick?.(c)}
                                className="rounded border border-teal-300 bg-white px-2 py-1 text-xs font-mono text-teal-900 hover:border-teal-500 hover:bg-teal-100"
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

export default DepthPickerCard;
