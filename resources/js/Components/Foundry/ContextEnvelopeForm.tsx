import { useState, useMemo, useCallback } from 'react';

/**
 * ContextEnvelopeForm — Phase 3 / Steps 3.2 + 3.3.
 *
 * The 12-field context envelope + Field/Office mode toggle that the
 * geologist attaches to a query. Lives directly above the chat textarea.
 * Default state is COLLAPSED — only the mode toggle is visible so simple
 * lookups don't pay UX tax. Expanding reveals every field; each blank
 * field is submitted as ``null`` (FastAPI side treats null as
 * "unspecified" and surfaces it in the OIUR uncertainty section per
 * Phase 2.4).
 *
 * Visual style matches the Foundry chat surface — Tailwind utilities +
 * `var(--*)` color tokens defined in resources/css/app.css.
 */

export type QueryMode = 'field' | 'office';

export type DepthReference = 'bgl' | 'asl' | 'rl' | 'tvd' | 'md';

export type DataSource =
    | 'drill_logs'
    | 'assays'
    | 'technical_reports'
    | 'maps'
    | 'geophysics'
    | 'public_geoscience';

export type ReportingCode =
    | 'NI 43-101'
    | 'CIM'
    | 'CRIRSCO'
    | 'JORC'
    | 'SAMREC'
    | 'PERC';

export interface ContextEnvelope {
    area_of_interest: string | null;
    crs_epsg: number | null;
    depth_reference: DepthReference | null;
    scale_resolution: string | null;
    stratigraphic_frame: string | null;
    specific_objects: string[];
    data_sources: DataSource[];
    qaqc_constraints: string | null;
    units_and_detection_limits: string | null;
    reporting_code: ReportingCode | null;
    decision_to_support: string | null;
    desired_output_structure: string | null;
    mode: QueryMode;
}

export const EMPTY_ENVELOPE: ContextEnvelope = {
    area_of_interest: null,
    crs_epsg: null,
    depth_reference: null,
    scale_resolution: null,
    stratigraphic_frame: null,
    specific_objects: [],
    data_sources: [],
    qaqc_constraints: null,
    units_and_detection_limits: null,
    reporting_code: null,
    decision_to_support: null,
    desired_output_structure: null,
    mode: 'office',
};

export interface ProjectContext {
    project_id: string;
    project_name: string;
    slug: string;
    crs_datum?: string | null;
    crs_epsg?: number | null;
    region?: string | null;
    commodity?: string | null;
}

interface Props {
    project: ProjectContext;
    value: ContextEnvelope;
    onChange: (envelope: ContextEnvelope) => void;
    disabled?: boolean;
}

const DATA_SOURCES: { value: DataSource; label: string }[] = [
    { value: 'drill_logs', label: 'Drill logs' },
    { value: 'assays', label: 'Assays' },
    { value: 'technical_reports', label: 'Technical reports' },
    { value: 'maps', label: 'Maps' },
    { value: 'geophysics', label: 'Geophysics' },
    { value: 'public_geoscience', label: 'Public geoscience' },
];

const REPORTING_CODES: ReportingCode[] = [
    'NI 43-101',
    'CIM',
    'CRIRSCO',
    'JORC',
    'SAMREC',
    'PERC',
];

const DEPTH_REFS: { value: DepthReference; label: string }[] = [
    { value: 'bgl', label: 'BGL (below ground level)' },
    { value: 'asl', label: 'ASL (above sea level)' },
    { value: 'rl', label: 'RL (reduced level)' },
    { value: 'tvd', label: 'TVD (true vertical depth)' },
    { value: 'md', label: 'MD (measured depth)' },
];

function fieldShellStyle() {
    return {
        background: 'var(--bg-2)',
        color: 'var(--fg-0)',
        borderColor: 'var(--line-2)',
    } as React.CSSProperties;
}

function labelStyle() {
    return {
        color: 'var(--fg-3)',
    } as React.CSSProperties;
}

function unspecifiedHint() {
    return {
        color: 'var(--fg-3)',
        fontStyle: 'italic',
        fontSize: '10px',
    } as React.CSSProperties;
}

/**
 * Smart defaults — the project's CRS pre-populates ``crs_epsg`` so the
 * UI shows "Pre-populated from project" instead of "unspecified" for that
 * field. The plan calls this out as an explicit UX contract: a populated
 * field reads as a value the geologist can override, NOT as a silent
 * default.
 */
export function applySmartDefaults(
    envelope: ContextEnvelope,
    project: ProjectContext,
): ContextEnvelope {
    if (envelope.crs_epsg === null && project.crs_epsg != null) {
        return { ...envelope, crs_epsg: project.crs_epsg };
    }
    return envelope;
}

export function ContextEnvelopeForm({
    project,
    value,
    onChange,
    disabled = false,
}: Props) {
    const [expanded, setExpanded] = useState(false);

    const populatedCount = useMemo(() => {
        let n = 0;
        if (value.area_of_interest) n++;
        if (value.crs_epsg !== null) n++;
        if (value.depth_reference) n++;
        if (value.scale_resolution) n++;
        if (value.stratigraphic_frame) n++;
        if (value.specific_objects.length > 0) n++;
        if (value.data_sources.length > 0) n++;
        if (value.qaqc_constraints) n++;
        if (value.units_and_detection_limits) n++;
        if (value.reporting_code) n++;
        if (value.decision_to_support) n++;
        if (value.desired_output_structure) n++;
        return n;
    }, [value]);

    const update = useCallback(
        <K extends keyof ContextEnvelope>(key: K, val: ContextEnvelope[K]) => {
            onChange({ ...value, [key]: val });
        },
        [onChange, value],
    );

    const updateDataSources = useCallback(
        (source: DataSource, checked: boolean) => {
            const next = checked
                ? Array.from(new Set([...value.data_sources, source]))
                : value.data_sources.filter((s) => s !== source);
            onChange({ ...value, data_sources: next });
        },
        [onChange, value],
    );

    const updateSpecificObjects = useCallback(
        (raw: string) => {
            const items = raw
                .split(/[,\n]/)
                .map((s) => s.trim())
                .filter(Boolean);
            onChange({ ...value, specific_objects: items });
        },
        [onChange, value],
    );

    const onModeToggle = useCallback(
        (mode: QueryMode) => {
            onChange({ ...value, mode });
        },
        [onChange, value],
    );

    return (
        <div
            className="border rounded mb-2"
            style={{
                background: 'var(--bg-1)',
                borderColor: 'var(--line-1)',
            }}
        >
            {/* Header row — mode toggle + expand/collapse */}
            <div className="flex items-center gap-2 px-3 py-2">
                <button
                    type="button"
                    onClick={() => setExpanded((x) => !x)}
                    disabled={disabled}
                    aria-expanded={expanded}
                    className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border"
                    style={{
                        color: 'var(--fg-2)',
                        background: 'var(--bg-2)',
                        borderColor: 'var(--line-2)',
                    }}
                >
                    {expanded ? '▾' : '▸'} Context
                    <span className="ml-1.5" style={{ color: 'var(--fg-3)' }}>
                        {populatedCount}/12
                    </span>
                </button>

                {/* Field / Office mode toggle */}
                <div
                    className="flex items-center text-[10px] font-mono uppercase tracking-wider rounded border overflow-hidden"
                    role="radiogroup"
                    aria-label="Query mode"
                    style={{ borderColor: 'var(--line-2)' }}
                >
                    <button
                        type="button"
                        role="radio"
                        aria-checked={value.mode === 'office'}
                        onClick={() => onModeToggle('office')}
                        disabled={disabled}
                        className="px-2 py-1"
                        style={{
                            background:
                                value.mode === 'office'
                                    ? 'var(--accent-bg)'
                                    : 'var(--bg-2)',
                            color:
                                value.mode === 'office'
                                    ? 'var(--accent)'
                                    : 'var(--fg-2)',
                        }}
                    >
                        Office
                    </button>
                    <button
                        type="button"
                        role="radio"
                        aria-checked={value.mode === 'field'}
                        onClick={() => onModeToggle('field')}
                        disabled={disabled}
                        className="px-2 py-1"
                        style={{
                            background:
                                value.mode === 'field'
                                    ? 'var(--accent-bg)'
                                    : 'var(--bg-2)',
                            color:
                                value.mode === 'field'
                                    ? 'var(--accent)'
                                    : 'var(--fg-2)',
                        }}
                    >
                        Field
                    </button>
                </div>

                {value.mode === 'field' && (
                    <span
                        className="text-[10px] font-mono uppercase tracking-wider"
                        style={{ color: 'var(--warn)' }}
                        title="Field mode: project corpus only, top-3 chunks, max 300 words"
                    >
                        ⚠ corpus-only · top-3 · 300w cap
                    </span>
                )}
            </div>

            {/* Expanded body — 12 fields grouped into 4 logical sections */}
            {expanded && (
                <div
                    className="border-t px-3 py-2 grid grid-cols-2 gap-x-3 gap-y-2"
                    style={{ borderColor: 'var(--line-1)' }}
                >
                    {/* Section 1: Spatial */}
                    <div className="col-span-2 text-[9px] font-mono uppercase tracking-[0.12em] mt-1" style={labelStyle()}>
                        Spatial
                    </div>

                    <TextField
                        label="Area of interest"
                        placeholder='e.g. "Within 5 km of DDH-07"'
                        value={value.area_of_interest ?? ''}
                        onChange={(v) =>
                            update('area_of_interest', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />
                    <NumberField
                        label="CRS / datum (EPSG)"
                        placeholder={
                            project.crs_epsg != null
                                ? `pre-populated: ${project.crs_epsg}`
                                : '1024–32767'
                        }
                        value={value.crs_epsg}
                        onChange={(v) => update('crs_epsg', v)}
                        disabled={disabled}
                    />
                    <SelectField
                        label="Depth reference"
                        value={value.depth_reference}
                        options={DEPTH_REFS}
                        onChange={(v) => update('depth_reference', v as DepthReference | null)}
                        disabled={disabled}
                    />
                    <TextField
                        label="Scale / resolution"
                        placeholder='e.g. "1:50,000 and finer"'
                        value={value.scale_resolution ?? ''}
                        onChange={(v) =>
                            update('scale_resolution', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />

                    {/* Section 2: Domain */}
                    <div className="col-span-2 text-[9px] font-mono uppercase tracking-[0.12em] mt-2" style={labelStyle()}>
                        Domain
                    </div>

                    <TextField
                        label="Stratigraphic / time frame"
                        placeholder='e.g. "ICS 2024; Watrous Formation"'
                        value={value.stratigraphic_frame ?? ''}
                        onChange={(v) =>
                            update('stratigraphic_frame', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />
                    <TextField
                        label="Specific objects"
                        placeholder='e.g. "DDH-07, DDH-08, DDH-12"'
                        value={value.specific_objects.join(', ')}
                        onChange={updateSpecificObjects}
                        disabled={disabled}
                    />
                    <div className="col-span-2">
                        <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={labelStyle()}>
                            Data sources to search
                            {value.data_sources.length === 0 && (
                                <span className="ml-1.5" style={unspecifiedHint()}>
                                    (unspecified — all surfaces)
                                </span>
                            )}
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                            {DATA_SOURCES.map((opt) => (
                                <label
                                    key={opt.value}
                                    className="flex items-center gap-1 text-xs px-2 py-1 rounded border cursor-pointer"
                                    style={fieldShellStyle()}
                                >
                                    <input
                                        type="checkbox"
                                        checked={value.data_sources.includes(opt.value)}
                                        onChange={(e) =>
                                            updateDataSources(opt.value, e.target.checked)
                                        }
                                        disabled={disabled}
                                    />
                                    {opt.label}
                                </label>
                            ))}
                        </div>
                    </div>

                    {/* Section 3: QA/QC + units */}
                    <div className="col-span-2 text-[9px] font-mono uppercase tracking-[0.12em] mt-2" style={labelStyle()}>
                        QA/QC + units
                    </div>

                    <TextField
                        label="QA/QC constraints"
                        placeholder='e.g. "Exclude batches failing CRM tolerance"'
                        value={value.qaqc_constraints ?? ''}
                        onChange={(v) =>
                            update('qaqc_constraints', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />
                    <TextField
                        label="Units and detection limits"
                        placeholder='e.g. "Cu in ppm; values <DL as half-DL"'
                        value={value.units_and_detection_limits ?? ''}
                        onChange={(v) =>
                            update('units_and_detection_limits', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />

                    {/* Section 4: Output */}
                    <div className="col-span-2 text-[9px] font-mono uppercase tracking-[0.12em] mt-2" style={labelStyle()}>
                        Output
                    </div>

                    <SelectField
                        label="Reporting code"
                        value={value.reporting_code}
                        options={REPORTING_CODES.map((c) => ({ value: c, label: c }))}
                        onChange={(v) => update('reporting_code', v as ReportingCode | null)}
                        disabled={disabled}
                        placeholder="(unspecified — defaults to NI 43-101)"
                    />
                    <TextField
                        label="Decision to support"
                        placeholder='e.g. "Rank infill drill targets"'
                        value={value.decision_to_support ?? ''}
                        onChange={(v) =>
                            update('decision_to_support', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />
                    <TextField
                        label="Desired output structure"
                        placeholder='e.g. "Interval table + confidence + citations"'
                        value={value.desired_output_structure ?? ''}
                        onChange={(v) =>
                            update('desired_output_structure', v.trim() === '' ? null : v)
                        }
                        disabled={disabled}
                    />
                </div>
            )}
        </div>
    );
}

/* ---------------- Field primitives ---------------- */

function TextField({
    label,
    placeholder,
    value,
    onChange,
    disabled,
}: {
    label: string;
    placeholder: string;
    value: string;
    onChange: (v: string) => void;
    disabled?: boolean;
}) {
    return (
        <label className="flex flex-col gap-0.5">
            <span
                className="text-[10px] font-mono uppercase tracking-wider"
                style={labelStyle()}
            >
                {label}
                {value === '' && (
                    <span className="ml-1.5" style={unspecifiedHint()}>
                        (unspecified)
                    </span>
                )}
            </span>
            <input
                type="text"
                value={value}
                placeholder={placeholder}
                onChange={(e) => onChange(e.target.value)}
                disabled={disabled}
                className="text-xs px-2 py-1 rounded border disabled:opacity-60"
                style={fieldShellStyle()}
            />
        </label>
    );
}

function NumberField({
    label,
    placeholder,
    value,
    onChange,
    disabled,
}: {
    label: string;
    placeholder: string;
    value: number | null;
    onChange: (v: number | null) => void;
    disabled?: boolean;
}) {
    return (
        <label className="flex flex-col gap-0.5">
            <span
                className="text-[10px] font-mono uppercase tracking-wider"
                style={labelStyle()}
            >
                {label}
                {value === null && (
                    <span className="ml-1.5" style={unspecifiedHint()}>
                        (unspecified)
                    </span>
                )}
            </span>
            <input
                type="number"
                value={value ?? ''}
                placeholder={placeholder}
                onChange={(e) => {
                    const raw = e.target.value.trim();
                    if (raw === '') {
                        onChange(null);
                        return;
                    }
                    const n = Number(raw);
                    onChange(Number.isFinite(n) ? n : null);
                }}
                disabled={disabled}
                className="text-xs px-2 py-1 rounded border disabled:opacity-60"
                style={fieldShellStyle()}
            />
        </label>
    );
}

function SelectField({
    label,
    value,
    options,
    onChange,
    disabled,
    placeholder = '(unspecified)',
}: {
    label: string;
    value: string | null;
    options: { value: string; label: string }[];
    onChange: (v: string | null) => void;
    disabled?: boolean;
    placeholder?: string;
}) {
    return (
        <label className="flex flex-col gap-0.5">
            <span
                className="text-[10px] font-mono uppercase tracking-wider"
                style={labelStyle()}
            >
                {label}
                {value === null && (
                    <span className="ml-1.5" style={unspecifiedHint()}>
                        (unspecified)
                    </span>
                )}
            </span>
            <select
                value={value ?? ''}
                onChange={(e) =>
                    onChange(e.target.value === '' ? null : e.target.value)
                }
                disabled={disabled}
                className="text-xs px-2 py-1 rounded border disabled:opacity-60"
                style={fieldShellStyle()}
            >
                <option value="">{placeholder}</option>
                {options.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                        {opt.label}
                    </option>
                ))}
            </select>
        </label>
    );
}
