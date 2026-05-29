/**
 * RefusalBanner — plan §4d UI surface for "no answer" cases.
 *
 * Used for: NO_EVIDENCE_FOUND, CITATION_INCOMPLETE, UNSUPPORTED_QUERY_TYPE.
 *
 * Visual chrome: gray neutral banner with an info icon. NOT red — red
 * is reserved for SOURCE_SCOPE_VIOLATION (see IncidentReportBanner).
 *
 * Optional reword prompt: when `onReword` is provided, a "Rephrase
 * question" link appears so the geologist can iterate without
 * retyping.
 */

import type { GuardPlaceholders } from "./GuardErrorMessage";
import { GuardErrorMessage } from "./GuardErrorMessage";

export interface RefusalBannerProps {
    code: string;
    placeholders?: GuardPlaceholders;
    onReword?: () => void;
}

export function RefusalBanner({
    code,
    placeholders = {},
    onReword,
}: RefusalBannerProps): JSX.Element {
    return (
        <div
            role="status"
            data-guard-surface="refusal"
            className="rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700"
        >
            <p>
                <GuardErrorMessage code={code} placeholders={placeholders} />
            </p>
            {onReword && (
                <button
                    type="button"
                    onClick={onReword}
                    className="mt-2 text-sm text-indigo-600 underline hover:text-indigo-800"
                >
                    Rephrase question
                </button>
            )}
        </div>
    );
}

export default RefusalBanner;
