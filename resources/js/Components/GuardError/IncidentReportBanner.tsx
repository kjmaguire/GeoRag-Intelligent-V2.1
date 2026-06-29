/**
 * IncidentReportBanner — plan §4d UI surface for SOURCE_SCOPE_VIOLATION.
 *
 * The ONE guard surface that warrants red chrome. Plan §5c lists
 * SOURCE_SCOPE_VIOLATION as a CRITICAL alert (workspace data isolation
 * almost-failure). The Sentry event fires automatically server-side;
 * this UI lets the user optionally file an incident report from their
 * side.
 *
 * Per plan §4d Q20: "button only, support form linked but not forced".
 * The button POSTs to the support endpoint when clicked; user can
 * dismiss without sending if they want.
 */

import type { JSX } from "react";
import { GuardErrorMessage } from "./GuardErrorMessage";

export interface IncidentReportBannerProps {
    /**
     * Fires when the user clicks "Report incident". Caller POSTs to
     * the support endpoint; UI provides visual feedback (loading /
     * sent / dismissed).
     */
    onReport?: () => void;
    /**
     * Fires when the user dismisses without reporting. Sentry already
     * captured the event; this is purely a UI gesture.
     */
    onDismiss?: () => void;
    /**
     * True when the report POST is in flight. Disables the button.
     */
    reporting?: boolean;
}

export function IncidentReportBanner({
    onReport,
    onDismiss,
    reporting = false,
}: IncidentReportBannerProps): JSX.Element {
    return (
        <div
            role="alert"
            data-guard-surface="incident"
            className="rounded-md border-2 border-red-300 bg-red-50 p-4 text-sm text-red-900"
        >
            <p className="font-semibold">Workspace isolation alert</p>
            <p className="mt-1">
                <GuardErrorMessage
                    code="SOURCE_SCOPE_VIOLATION"
                    placeholders={{}}
                />
            </p>
            <div className="mt-3 flex gap-2">
                {onReport && (
                    <button
                        type="button"
                        onClick={onReport}
                        disabled={reporting}
                        className="rounded bg-red-600 px-3 py-1 text-xs font-semibold text-white hover:bg-red-700 disabled:opacity-50"
                    >
                        {reporting ? "Reporting…" : "Report incident"}
                    </button>
                )}
                {onDismiss && (
                    <button
                        type="button"
                        onClick={onDismiss}
                        disabled={reporting}
                        className="rounded border border-red-300 bg-white px-3 py-1 text-xs text-red-900 hover:bg-red-100 disabled:opacity-50"
                    >
                        Dismiss
                    </button>
                )}
            </div>
        </div>
    );
}

export default IncidentReportBanner;
