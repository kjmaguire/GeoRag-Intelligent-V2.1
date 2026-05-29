/**
 * ConflictSideBySide — plan §4d UI surface for CONFLICTING_SOURCES.
 *
 * Renders two documents disagreeing on a value, side-by-side. Picks
 * the `_WITH_AUTHORITY` degradation automatically when an
 * `authoritativeDoc` prop is supplied (e.g. when plan §1h supersession
 * has flagged a winner).
 *
 * Per plan §4d Global Invariant 7: NEVER silently pick a winner. Both
 * documents are shown; the authoritative_doc flag adds a label, not a
 * filter.
 */

import { GuardErrorMessage } from "./GuardErrorMessage";

export interface ConflictSideBySideProps {
    /**
     * The two documents in conflict. `name` is shown as the column
     * heading; `value` is the value the document carries; optional
     * `citation` renders a small subscript.
     */
    documentA: { name: string; value: string; citation?: string };
    documentB: { name: string; value: string; citation?: string };
    /**
     * Optional short interpretation of WHY the values differ.
     * Renders below the two columns when supplied.
     */
    interpretation?: string;
    /**
     * When set, the renderer picks CONFLICTING_SOURCES_WITH_AUTHORITY
     * and labels the matching column as "current".
     */
    authoritativeDoc?: string;
}

export function ConflictSideBySide({
    documentA,
    documentB,
    interpretation,
    authoritativeDoc,
}: ConflictSideBySideProps): JSX.Element {
    const placeholders = {
        document_a: documentA.name,
        document_b: documentB.name,
        value_a: documentA.value,
        value_b: documentB.value,
        interpretation_or_rounding: interpretation ?? "an updated estimate",
        authoritative_doc: authoritativeDoc,
    };

    const isAuthoritative = (docName: string): boolean =>
        Boolean(authoritativeDoc) && docName === authoritativeDoc;

    return (
        <div
            data-guard-surface="conflict"
            className="rounded-md border border-orange-200 bg-orange-50 p-4 text-sm text-orange-900"
        >
            <p className="mb-3 font-medium">Two sources disagree</p>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {[documentA, documentB].map((doc) => (
                    <div
                        key={doc.name}
                        className="rounded border border-orange-200 bg-white p-3"
                        data-document={doc.name}
                    >
                        <div className="mb-1 flex items-center justify-between">
                            <span className="text-xs font-semibold text-orange-900">
                                {doc.name}
                            </span>
                            {isAuthoritative(doc.name) && (
                                <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-800">
                                    Current
                                </span>
                            )}
                        </div>
                        <div className="text-base font-medium text-slate-900">
                            {doc.value}
                        </div>
                        {doc.citation && (
                            <div className="mt-1 text-xs text-slate-500">
                                {doc.citation}
                            </div>
                        )}
                    </div>
                ))}
            </div>
            <p className="mt-3 text-xs text-orange-800">
                <GuardErrorMessage
                    code="CONFLICTING_SOURCES"
                    placeholders={placeholders}
                />
            </p>
        </div>
    );
}

export default ConflictSideBySide;
