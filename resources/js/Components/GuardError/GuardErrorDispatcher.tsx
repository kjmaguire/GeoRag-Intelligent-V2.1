/**
 * GuardErrorDispatcher — plan §4d entry point.
 *
 * Maps a `GuardErrorCode` to its appropriate surface component. Use
 * this from the chat message renderer (or any assistant-response
 * wrapper) when a response carries `guard_error_codes`.
 *
 * Surface map (per `docs/architecture/user_facing_error_catalog.md`):
 *
 *   refusal    → NO_EVIDENCE_FOUND, CITATION_INCOMPLETE,
 *                UNSUPPORTED_QUERY_TYPE
 *   ambiguity  → AMBIGUOUS_HOLE_ID, AMBIGUOUS_FORMATION_NAME,
 *                AMBIGUOUS_PROPERTY_NAME
 *   conflict   → CONFLICTING_SOURCES
 *   partial    → NUMERIC_GROUNDING_FAILED, MISSING_DEPTH_INTERVAL,
 *                MISSING_ASSAY_UNITS, OVER_FILTERED_QUERY
 *   incident   → SOURCE_SCOPE_VIOLATION
 *   spatial    → SPATIAL_CRS_MISMATCH, SPATIAL_QUERY_EMPTY
 *                (rendered as refusal for v1; specialised surface TBD)
 *   graph      → GRAPH_PATH_NOT_FOUND
 *                (rendered as refusal for v1)
 *   death_loop → DEATH_LOOP (rendered as refusal)
 *
 * `surfaceFor(code)` is exposed so callers can also dispatch to their
 * own components by surface kind rather than by code.
 */

import type { JSX } from "react";
import { AmbiguityPicker } from "./AmbiguityPicker";
import { ConflictSideBySide } from "./ConflictSideBySide";
import { DepthPickerCard } from "./DepthPickerCard";
import type { GuardPlaceholders } from "./GuardErrorMessage";
import { IncidentReportBanner } from "./IncidentReportBanner";
import { PartialAnswerCard } from "./PartialAnswerCard";
import { RefusalBanner } from "./RefusalBanner";
import { UnitPickerCard } from "./UnitPickerCard";

export type GuardSurfaceKind =
    | "refusal"
    | "ambiguity"
    | "conflict"
    | "partial"
    | "incident"
    | "unit-picker"
    | "depth-picker";

const CODE_TO_SURFACE: Record<string, GuardSurfaceKind> = {
    NO_EVIDENCE_FOUND: "refusal",
    CITATION_INCOMPLETE: "refusal",
    UNSUPPORTED_QUERY_TYPE: "refusal",
    SPATIAL_QUERY_EMPTY: "refusal",
    SPATIAL_CRS_MISMATCH: "refusal",
    GRAPH_PATH_NOT_FOUND: "refusal",
    DEATH_LOOP: "refusal",

    AMBIGUOUS_HOLE_ID: "ambiguity",
    AMBIGUOUS_FORMATION_NAME: "ambiguity",
    AMBIGUOUS_PROPERTY_NAME: "ambiguity",

    CONFLICTING_SOURCES: "conflict",

    NUMERIC_GROUNDING_FAILED: "partial",
    OVER_FILTERED_QUERY: "partial",

    // Plan §4b terminal strategies — REQUEST_UNIT_CLARIFICATION /
    // REQUEST_DEPTH_CLARIFICATION surfaces. The corresponding
    // GuardErrorCode entries route here too so the user sees the
    // picker rather than a generic partial-answer card.
    MISSING_ASSAY_UNITS: "unit-picker",
    REQUEST_UNIT_CLARIFICATION: "unit-picker",
    MISSING_DEPTH_INTERVAL: "depth-picker",
    REQUEST_DEPTH_CLARIFICATION: "depth-picker",

    SOURCE_SCOPE_VIOLATION: "incident",
};

/**
 * Lookup the surface kind for a code. Unknown codes default to
 * "refusal" (safe default — renders the catalog text in neutral
 * chrome with no special interaction).
 */
export function surfaceFor(code: string): GuardSurfaceKind {
    return CODE_TO_SURFACE[code] ?? "refusal";
}

export interface GuardErrorDispatcherProps {
    code: string;
    placeholders?: GuardPlaceholders;
    /**
     * For AmbiguityPicker. Required when the code maps to "ambiguity"
     * surface; ignored otherwise.
     */
    candidates?: string[];
    /**
     * The ambiguous term itself (e.g. "ECK-22-001"). Required for
     * ambiguity surface.
     */
    term?: string;
    onPick?: (candidate: string) => void;
    onReword?: () => void;
    /**
     * For ConflictSideBySide. Required when code = CONFLICTING_SOURCES.
     */
    conflictDocuments?: {
        documentA: { name: string; value: string; citation?: string };
        documentB: { name: string; value: string; citation?: string };
        interpretation?: string;
        authoritativeDoc?: string;
    };
    /**
     * For PartialAnswerCard. Required when code maps to "partial".
     */
    partial?: {
        answer: React.ReactNode;
        reason: string;
        evidenceFound?: string;
        missing?: string;
        suggestion?: string;
    };
    /**
     * For IncidentReportBanner. Required when code = SOURCE_SCOPE_VIOLATION.
     */
    onReport?: () => void;
    onDismiss?: () => void;
    reporting?: boolean;
    /**
     * For UnitPickerCard. Optional — the picker has sensible defaults
     * (g/t / ppm / ppb / wt% / %) when omitted.
     */
    unitCandidates?: string[];
    commodity?: string;
    /**
     * For DepthPickerCard. Optional — picker defaults to m / ft when
     * omitted.
     */
    depthCandidates?: string[];
    depthValue?: string;
}

/**
 * Dispatch to the right surface component based on the code's surface
 * kind. Missing props for the selected surface degrade gracefully —
 * AmbiguityPicker with no candidates falls back to a RefusalBanner
 * containing the disambiguation prompt, etc.
 */
export function GuardErrorDispatcher(props: GuardErrorDispatcherProps): JSX.Element {
    const surface = surfaceFor(props.code);

    if (surface === "ambiguity" && props.candidates && props.term) {
        return (
            <AmbiguityPicker
                code={props.code}
                term={props.term}
                candidates={props.candidates}
                onPick={props.onPick}
                placeholders={props.placeholders}
            />
        );
    }

    if (surface === "conflict" && props.conflictDocuments) {
        return <ConflictSideBySide {...props.conflictDocuments} />;
    }

    if (surface === "partial" && props.partial) {
        return (
            <PartialAnswerCard
                code={props.code}
                reason={props.partial.reason}
                evidenceFound={props.partial.evidenceFound}
                missing={props.partial.missing}
                suggestion={props.partial.suggestion}
            >
                {props.partial.answer}
            </PartialAnswerCard>
        );
    }

    if (surface === "incident") {
        return (
            <IncidentReportBanner
                onReport={props.onReport}
                onDismiss={props.onDismiss}
                reporting={props.reporting}
            />
        );
    }

    if (surface === "unit-picker") {
        return (
            <UnitPickerCard
                code={props.code}
                commodity={props.commodity}
                candidates={props.unitCandidates}
                onPick={props.onPick}
                placeholders={props.placeholders}
            />
        );
    }

    if (surface === "depth-picker") {
        return (
            <DepthPickerCard
                code={props.code}
                value={props.depthValue}
                candidates={props.depthCandidates}
                onPick={props.onPick}
                placeholders={props.placeholders}
            />
        );
    }

    // Default + missing-props fallback: RefusalBanner.
    return (
        <RefusalBanner
            code={props.code}
            placeholders={props.placeholders}
            onReword={props.onReword}
        />
    );
}

export default GuardErrorDispatcher;
