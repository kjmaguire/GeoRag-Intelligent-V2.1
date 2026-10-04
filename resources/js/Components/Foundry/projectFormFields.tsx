import type { CSSProperties, ReactNode } from 'react';

/**
 * Form pieces shared by the New Project wizard (Pages/Foundry/NewProject.tsx)
 * and the Edit project sheet (Components/EditProjectSheet.tsx), so the two
 * surfaces offer the same commodity list and look the same.
 *
 * `commodity` is an open vocabulary (§04e — a plain, unvalidated string);
 * this list is only what the picker offers. Values are stored lower-cased.
 */
export const COMMODITIES = ['Uranium', 'Gold', 'Copper', 'Nickel', 'Lithium', 'Zinc', 'Silver', 'Lead', 'REE'];

export const inputStyle = {
    background: 'var(--bg-2)',
    color: 'var(--fg-0)',
    borderColor: 'var(--line-2)',
} as CSSProperties;

export function Field({ label, required, children }: { label: string; required?: boolean; children: ReactNode }) {
    return (
        <label className="block">
            <span
                className="text-[10px] font-mono uppercase tracking-wider mb-1 block"
                style={{ color: 'var(--fg-3)' }}
            >
                {label}
                {required && <span style={{ color: 'var(--accent)' }}> *</span>}
            </span>
            {children}
        </label>
    );
}

/**
 * silver.projects.orientation_reference — mirrors App\Models\Project::ORIENTATION_REFERENCES.
 *
 * BOH / TOH are the core-orientation mark (bottom / top of hole) and declare
 * no azimuth north, so desurvey leaves azimuths alone. grid / true / magnetic
 * declare which north the project's survey azimuths are measured from, and
 * desurvey converts them to the collar's local grid (true: the meridian
 * convergence; magnetic: the declination plus the convergence). A survey
 * file's own azimuth-reference column (Azimuth_Ref, North_Ref...) wins over
 * this per station.
 */
export type OrientationReference = 'BOH' | 'TOH' | 'grid' | 'true' | 'magnetic';

export const ORIENTATION_REFERENCE_GROUPS: ReadonlyArray<{
    label: string;
    options: ReadonlyArray<{ value: OrientationReference; label: string }>;
}> = [
    {
        label: 'Core orientation mark — azimuths not corrected',
        options: [
            { value: 'BOH', label: 'BOH — bottom of hole' },
            { value: 'TOH', label: 'TOH — top of hole' },
        ],
    },
    {
        label: 'Azimuth north reference',
        options: [
            { value: 'grid', label: 'Grid north (project coordinate system)' },
            { value: 'true', label: 'True north' },
            { value: 'magnetic', label: 'Magnetic north' },
        ],
    },
];

const ORIENTATION_VALUES: ReadonlyArray<string> = ORIENTATION_REFERENCE_GROUPS.flatMap((g) =>
    g.options.map((o) => o.value),
);

/**
 * A stored orientation_reference as a value the picker offers. The LAS /
 * cluster ingestion stubs wrote 'grid_north' before 2026-09-29, which the
 * desurvey code already reads as grid; anything else unknown shows as BOH
 * (declares nothing), the column's default.
 */
export function normaliseOrientationReference(stored: string | null | undefined): OrientationReference {
    if (stored === 'grid_north') return 'grid';
    return (ORIENTATION_VALUES.includes(stored ?? '') ? stored : 'BOH') as OrientationReference;
}

/**
 * Parse the declination box: degrees, EAST positive, west negative.
 * Blank is "not recorded" (undefined), which is not 0.
 */
export function parseDeclination(text: string): { value?: number; error?: string } {
    const trimmed = text.trim();
    if (trimmed === '') return {};
    if (!/^[+-]?\d+(\.\d+)?$/.test(trimmed)) {
        return { error: 'Declination is a number of degrees, east positive (e.g. 14.5 or -17).' };
    }
    const value = Number(trimmed);
    if (value < -180 || value > 180) return { error: 'Declination must be between -180 and 180 degrees.' };
    return { value };
}

/** The error to show for a reference + declination pair, if any. */
export function declinationError(reference: OrientationReference, declinationText: string): string | undefined {
    const parsed = parseDeclination(declinationText);
    if (parsed.error) return parsed.error;
    if (reference === 'magnetic' && parsed.value === undefined) {
        return 'Magnetic north needs a declination (degrees, east positive).';
    }
    return undefined;
}

export function AzimuthReferenceFields({
    reference,
    declination,
    onReferenceChange,
    onDeclinationChange,
    referenceError,
    declinationServerError,
}: {
    reference: OrientationReference;
    declination: string;
    onReferenceChange: (value: OrientationReference) => void;
    onDeclinationChange: (value: string) => void;
    referenceError?: string;
    declinationServerError?: string;
}) {
    const shownError = declinationServerError ?? declinationError(reference, declination);
    return (
        <div className="space-y-4" data-testid="azimuth-reference-fields">
            <Field label="Azimuth reference">
                <select
                    name="orientation_reference"
                    value={reference}
                    onChange={(e) => onReferenceChange(e.target.value as OrientationReference)}
                    aria-invalid={referenceError ? true : undefined}
                    className="w-full text-sm px-3 py-2 rounded border"
                    style={inputStyle}
                >
                    {ORIENTATION_REFERENCE_GROUPS.map((group) => (
                        <optgroup key={group.label} label={group.label}>
                            {group.options.map((o) => (
                                <option key={o.value} value={o.value}>
                                    {o.label}
                                </option>
                            ))}
                        </optgroup>
                    ))}
                </select>
                <p className="mt-1 text-[11px] leading-relaxed" style={{ color: 'var(--fg-3)' }}>
                    Which north your survey azimuths are measured from. True and magnetic are converted to grid when
                    drill traces are built; BOH / TOH and grid leave them as recorded. A survey file with its own
                    reference column (e.g. <code>Azimuth_Ref</code>) overrides this for its stations.
                </p>
                {referenceError && (
                    <p role="alert" className="mt-1 text-[11px]" style={{ color: 'var(--danger, #f87171)' }}>
                        {referenceError}
                    </p>
                )}
            </Field>
            {(reference === 'magnetic' || declination.trim() !== '') && (
                <Field label="Magnetic declination (degrees, east positive)" required={reference === 'magnetic'}>
                    <input
                        type="text"
                        inputMode="decimal"
                        name="magnetic_declination"
                        value={declination}
                        onChange={(e) => onDeclinationChange(e.target.value)}
                        placeholder="e.g. 14.5 (east) or -17 (west)"
                        aria-invalid={shownError ? true : undefined}
                        className="w-full text-sm px-3 py-2 rounded border"
                        style={inputStyle}
                    />
                    <p className="mt-1 text-[11px] leading-relaxed" style={{ color: 'var(--fg-3)' }}>
                        East of true north is positive, west is negative. Use the declination for the survey epoch; it
                        drifts by tenths of a degree a year.
                    </p>
                    {shownError && (
                        <p role="alert" className="mt-1 text-[11px]" style={{ color: 'var(--danger, #f87171)' }}>
                            {shownError}
                        </p>
                    )}
                </Field>
            )}
        </div>
    );
}
