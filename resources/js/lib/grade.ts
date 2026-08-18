/**
 * Formatting for assay grade values.
 *
 * Exists to pin one unit contract in a single place. `mean_u3o8_pct` (and the
 * underlying `silver.samples.commodity_assays->>'U3O8_pct_e'`) is ALREADY a
 * percentage — not a 0-1 fraction. The authority is
 * `src/fastapi/app/services/ingest/derive_intervals.py`, whose classifier
 * labels an interval ORE at `grade > 0.02`, documented in that module as
 * "GRADE > 0.02 %eU3O8" — a 0.02 percent cutoff.
 *
 * This was previously formatted inline in two components that disagreed:
 * WorkspaceMap rendered it unscaled (correct) while CompareHolesModal
 * multiplied by 100, so the hole-comparison view overstated every grade by
 * 100x — a 0.35% U₃O₈ intercept displayed as "35.000%". Both now call this.
 */

/** Decimal places used for U₃O₈ grades; matches the 5-dp payload rounding. */
const U3O8_DECIMALS = 3;

/**
 * Render a U₃O₈ grade already expressed in percent.
 *
 * @param value percent eU₃O₈ (e.g. 0.35 means 0.35%), or null when unknown.
 * @param placeholder shown when value is null.
 */
export function formatU3O8Pct(value: number | null | undefined, placeholder = '—'): string {
    if (value === null || value === undefined || Number.isNaN(value)) {
        return placeholder;
    }
    return `${value.toFixed(U3O8_DECIMALS)}%`;
}
