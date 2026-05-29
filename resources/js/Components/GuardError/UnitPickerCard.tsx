/**
 * UnitPickerCard — plan §4b/§4d UI surface for
 * REQUEST_UNIT_CLARIFICATION terminal strategy.
 *
 * When the agent can't disambiguate assay units between common
 * families (g/t Au vs ppm vs wt% vs % vs ppb), the dispatcher
 * surfaces this card. Each candidate unit is a clickable chip;
 * onPick fires when the geologist picks one, and the parent
 * re-issues the query with the unit fixed.
 *
 * ADR-0009 Stage 2 dependency — terminal surfaces light up before
 * any loop-friendly strategy enables. See
 * docs/architecture/repair_loop_spec.md §2 (terminal strategies).
 */

import type { GuardPlaceholders } from "./GuardErrorMessage";
import { GuardErrorMessage } from "./GuardErrorMessage";

export interface UnitPickerCardProps {
    code?: string;
    /**
     * The detected commodity (e.g. "Au"). Becomes the `:commodity`
     * placeholder in the message.
     */
    commodity?: string;
    /**
     * Candidate unit strings to render as chips. Defaults to the
     * five common assay families when not supplied. The
     * disambiguation message's `:candidates` placeholder is set to
     * a joined string of these.
     */
    candidates?: string[];
    onPick?: (candidate: string) => void;
    placeholders?: GuardPlaceholders;
}

const DEFAULT_UNIT_CANDIDATES: string[] = [
    "g/t",   // grams per tonne — most common precious-metal grade
    "ppm",   // parts per million — common for trace + base metals
    "ppb",   // parts per billion — high-purity precious-metal anomalies
    "wt%",   // weight percent — concentrate / high-grade massive sulphide
    "%",     // bare percent (legacy reports)
];

export function UnitPickerCard({
    code = "REQUEST_UNIT_CLARIFICATION",
    commodity = "",
    candidates = DEFAULT_UNIT_CANDIDATES,
    onPick,
    placeholders = {},
}: UnitPickerCardProps): JSX.Element {
    const mergedPlaceholders: GuardPlaceholders = {
        ...placeholders,
        commodity,
        candidates: candidates.join(", "),
    };

    return (
        <div
            data-guard-surface="unit-picker"
            className="rounded-md border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900"
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
                    aria-label="Unit candidates"
                >
                    {candidates.map((c) => (
                        <li key={c}>
                            <button
                                type="button"
                                onClick={() => onPick?.(c)}
                                className="rounded border border-sky-300 bg-white px-2 py-1 text-xs font-mono text-sky-900 hover:border-sky-500 hover:bg-sky-100"
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

export default UnitPickerCard;
