/**
 * PartialAnswerCard — plan §4d UI surface for "answered with caveats".
 *
 * Wraps an answer that came back with one or more evidence-quality
 * guard codes that didn't outright refuse but DID flag the answer as
 * partial. Used for:
 *   - NUMERIC_GROUNDING_FAILED (with repair attempted)
 *   - MISSING_DEPTH_INTERVAL
 *   - MISSING_ASSAY_UNITS
 *   - OVER_FILTERED_QUERY (relaxed filters)
 *
 * Render shape per plan §4d "Partial answer format":
 *
 *   Answer: <child content>
 *   ⚠ Confidence: Low — <reason>
 *   Evidence found: <evidenceFound>
 *   What's missing: <missing>
 *   Suggestion: <suggestion>
 *
 * Amber ⚠ badge — distinct from red (refusal) and gray (info).
 */

import type { ReactNode } from "react";

export interface PartialAnswerCardProps {
    /**
     * The answer body. Free-form ReactNode so callers can pass the
     * formatted markdown / structured-format render.
     */
    children: ReactNode;
    /**
     * The single GuardErrorCode that flagged this answer as partial.
     * Used as a data attribute for telemetry / styling.
     */
    code: string;
    /**
     * One-line reason for the Low confidence rating. From plan §4d:
     * "missing evidence / conflicting sources / no citations".
     */
    reason: string;
    /**
     * Short summary of what evidence the retrieval pipeline DID
     * surface. Renders under "Evidence found:".
     */
    evidenceFound?: string;
    /**
     * What the answer is missing. Renders under "What's missing:".
     */
    missing?: string;
    /**
     * Suggested next step. Renders under "Suggestion:".
     */
    suggestion?: string;
}

export function PartialAnswerCard({
    children,
    code,
    reason,
    evidenceFound,
    missing,
    suggestion,
}: PartialAnswerCardProps): JSX.Element {
    return (
        <div
            data-guard-surface="partial"
            data-guard-code={code}
            className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900"
        >
            <div className="mb-2">{children}</div>
            <div className="border-t border-amber-200 pt-2 text-xs">
                <p>
                    <span aria-label="warning" role="img">
                        ⚠
                    </span>{" "}
                    <span className="font-semibold">Confidence: Low</span>
                    {" — "}
                    <span>{reason}</span>
                </p>
                {evidenceFound && (
                    <p className="mt-1">
                        <span className="font-semibold">Evidence found:</span>{" "}
                        <span>{evidenceFound}</span>
                    </p>
                )}
                {missing && (
                    <p className="mt-1">
                        <span className="font-semibold">
                            What&apos;s missing:
                        </span>{" "}
                        <span>{missing}</span>
                    </p>
                )}
                {suggestion && (
                    <p className="mt-1">
                        <span className="font-semibold">Suggestion:</span>{" "}
                        <span>{suggestion}</span>
                    </p>
                )}
            </div>
        </div>
    );
}

export default PartialAnswerCard;
