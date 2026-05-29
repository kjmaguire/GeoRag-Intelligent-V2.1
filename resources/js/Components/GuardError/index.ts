/**
 * Plan §4d guard-error surface barrel.
 *
 * Import the dispatcher for ad-hoc rendering of any code:
 *
 *   import { GuardErrorDispatcher } from "@/Components/GuardError";
 *
 * Or grab specific surface components when you know which surface
 * you want:
 *
 *   import { ConflictSideBySide, RefusalBanner } from "@/Components/GuardError";
 *
 * The primitive `GuardErrorMessage` is the lowest-level renderer —
 * use it when you want the catalog text without any chrome.
 */

export { AmbiguityPicker } from "./AmbiguityPicker";
export type { AmbiguityPickerProps } from "./AmbiguityPicker";

export { ConflictSideBySide } from "./ConflictSideBySide";
export type { ConflictSideBySideProps } from "./ConflictSideBySide";

export {
    GuardErrorDispatcher,
    surfaceFor,
} from "./GuardErrorDispatcher";
export type {
    GuardErrorDispatcherProps,
    GuardSurfaceKind,
} from "./GuardErrorDispatcher";

export {
    ALL_GUARD_ERROR_CODES,
    GuardErrorMessage,
    resolveGuardErrorMessage,
} from "./GuardErrorMessage";
export type {
    GuardErrorCode,
    GuardErrorMessageProps,
    GuardPlaceholders,
    GuardPlaceholderValue,
} from "./GuardErrorMessage";

export { IncidentReportBanner } from "./IncidentReportBanner";
export type { IncidentReportBannerProps } from "./IncidentReportBanner";

export { PartialAnswerCard } from "./PartialAnswerCard";
export type { PartialAnswerCardProps } from "./PartialAnswerCard";

export { RefusalBanner } from "./RefusalBanner";
export type { RefusalBannerProps } from "./RefusalBanner";

export { UnitPickerCard } from "./UnitPickerCard";
export type { UnitPickerCardProps } from "./UnitPickerCard";

export { DepthPickerCard } from "./DepthPickerCard";
export type { DepthPickerCardProps } from "./DepthPickerCard";
