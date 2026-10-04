import { useEffect, useState, type FormEvent } from 'react';
import { router, useHttp } from '@inertiajs/react';
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from '@/Components/ui/sheet';
import {
    AzimuthReferenceFields,
    COMMODITIES,
    declinationError,
    Field,
    inputStyle,
    normaliseOrientationReference,
    type OrientationReference,
} from '@/Components/Foundry/projectFormFields';

export interface EditableProject {
    project_id: string;
    project_name: string;
    company: string | null;
    commodity: string | null;
    region: string | null;
    /** Shown read-only; see the component docblock. */
    crs_epsg?: number | null;
    /** BOH | TOH | grid | true | magnetic (legacy 'grid_north' reads as grid). */
    orientation_reference?: string | null;
    /** Degrees, east positive; null = not recorded. */
    magnetic_declination?: number | null;
}

interface EditProjectForm {
    project_name: string;
    company: string;
    commodity: string;
    region: string;
    orientation_reference: OrientationReference;
    /** The text box's contents; '' is sent and stored as NULL, never 0. */
    magnetic_declination: string;
}

interface EditProjectSheetProps {
    project: EditableProject;
    open: boolean;
    onOpenChange: (open: boolean) => void;
}

function formFrom(project: EditableProject): EditProjectForm {
    return {
        project_name: project.project_name,
        company: project.company ?? '',
        commodity: project.commodity ?? '',
        region: project.region ?? '',
        orientation_reference: normaliseOrientationReference(project.orientation_reference),
        magnetic_declination:
            project.magnetic_declination === null || project.magnetic_declination === undefined
                ? ''
                : String(project.magnetic_declination),
    };
}

/**
 * Edit project — a Sheet opened from the Overview header, beside Open Chat
 * and Delete Project.
 *
 * PATCHes the same `/api/v1/projects/{id}` route the Delete button's DELETE
 * uses (ProjectController::update, UpdateProjectRequest), so it sits behind
 * the identical membership gate. Inertia's `useHttp` rather than `useForm`:
 * the endpoint answers JSON, not an Inertia page, and `useHttp` still maps a
 * 422 onto per-field `errors`. On success the Overview's props are reloaded.
 *
 * Identity fields plus the azimuth reference (and magnetic declination,
 * degrees east positive) — see UpdateProjectRequest's docblock. The azimuth
 * reference is editable because desurvey re-applies it on the next promotion
 * and rebuilds the affected traces. The coordinate system is shown read-only
 * because changing it would not reproject the holes already ingested; the
 * slug (the URL) never changes.
 */
export default function EditProjectSheet({ project, open, onOpenChange }: EditProjectSheetProps) {
    const form = useHttp<EditProjectForm>(formFrom(project));
    const [failure, setFailure] = useState<string | null>(null);

    // Start from the project's current values every time the sheet opens, so
    // a cancelled edit (or one made in another tab) never lingers.
    useEffect(() => {
        if (!open) return;
        form.setData(formFrom(project));
        form.clearErrors();
        setFailure(null);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [
        open,
        project.project_id,
        project.project_name,
        project.company,
        project.commodity,
        project.region,
        project.orientation_reference,
        project.magnetic_declination,
    ]);

    const localDeclinationError = declinationError(form.data.orientation_reference, form.data.magnetic_declination);

    // Keep a legacy value (e.g. 'U3O8' from a seed) selectable instead of
    // silently blanking it the first time someone opens the sheet.
    const commodityOptions = COMMODITIES.map((c) => ({ value: c.toLowerCase(), label: c }));
    if (form.data.commodity && !commodityOptions.some((o) => o.value === form.data.commodity)) {
        commodityOptions.unshift({ value: form.data.commodity, label: form.data.commodity });
    }

    async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
        event.preventDefault();
        setFailure(null);
        // Magnetic with no declination would be stored and then silently not
        // applied; the server refuses it too (ProjectController::update).
        if (localDeclinationError) return;

        const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ?? null;

        try {
            await form.patch(`/api/v1/projects/${project.project_id}`, {
                headers: csrf ? { 'X-CSRF-TOKEN': csrf } : {},
                onSuccess: () => {
                    onOpenChange(false);
                    router.reload();
                },
                onHttpException: (response) => {
                    // Same surfacing as the Delete button: the controller
                    // returns a generic `message` plus the real `error`.
                    let detail = `HTTP ${response.status}`;
                    try {
                        const body = JSON.parse(response.data || '{}');
                        detail = [body.message, body.error].filter(Boolean).join(': ') || detail;
                    } catch {
                        // non-JSON body — keep the status line
                    }
                    setFailure(detail);
                },
                onNetworkError: (error) => setFailure(error.message),
            });
        } catch {
            // Already surfaced through onHttpException / onNetworkError.
        }
    }

    return (
        <Sheet open={open} onOpenChange={onOpenChange}>
            <SheetContent
                data-testid="edit-project-sheet"
                className="overflow-y-auto"
                style={{ background: 'var(--bg-1)', color: 'var(--fg-1)', borderColor: 'var(--line-1)' }}
            >
                <form onSubmit={submit} className="flex flex-col h-full" noValidate>
                    <SheetHeader>
                        <SheetTitle style={{ color: 'var(--fg-0)' }}>Edit project</SheetTitle>
                        <SheetDescription style={{ color: 'var(--fg-3)' }}>
                            Rename the project, correct its operator, commodity and region, or set which north its
                            survey azimuths use. The project URL stays the same.
                        </SheetDescription>
                    </SheetHeader>

                    <div className="px-4 space-y-4">
                        <Field label="Project name" required>
                            <input
                                type="text"
                                name="project_name"
                                value={form.data.project_name}
                                onChange={(e) => form.setData('project_name', e.target.value)}
                                maxLength={255}
                                aria-invalid={form.errors.project_name ? true : undefined}
                                className="w-full text-sm px-3 py-2 rounded border"
                                style={inputStyle}
                            />
                            <FieldError message={form.errors.project_name} />
                        </Field>

                        <Field label="Operator">
                            <input
                                type="text"
                                name="company"
                                value={form.data.company}
                                onChange={(e) => form.setData('company', e.target.value)}
                                maxLength={255}
                                aria-invalid={form.errors.company ? true : undefined}
                                className="w-full text-sm px-3 py-2 rounded border"
                                style={inputStyle}
                            />
                            <FieldError message={form.errors.company} />
                        </Field>

                        <Field label="Commodity">
                            <select
                                name="commodity"
                                value={form.data.commodity}
                                onChange={(e) => form.setData('commodity', e.target.value)}
                                aria-invalid={form.errors.commodity ? true : undefined}
                                className="w-full text-sm px-3 py-2 rounded border"
                                style={inputStyle}
                            >
                                <option value="">— none —</option>
                                {commodityOptions.map((o) => (
                                    <option key={o.value} value={o.value}>
                                        {o.label}
                                    </option>
                                ))}
                            </select>
                            <FieldError message={form.errors.commodity} />
                        </Field>

                        <Field label="Region">
                            <input
                                type="text"
                                name="region"
                                value={form.data.region}
                                onChange={(e) => form.setData('region', e.target.value)}
                                maxLength={255}
                                placeholder="e.g. WY or Saskatchewan"
                                aria-invalid={form.errors.region ? true : undefined}
                                className="w-full text-sm px-3 py-2 rounded border"
                                style={inputStyle}
                            />
                            <p className="mt-1 text-[11px]" style={{ color: 'var(--fg-3)' }}>
                                State or province code, as the New Project wizard writes it.
                            </p>
                            <FieldError message={form.errors.region} />
                        </Field>

                        <AzimuthReferenceFields
                            reference={form.data.orientation_reference}
                            declination={form.data.magnetic_declination}
                            onReferenceChange={(value) => form.setData('orientation_reference', value)}
                            onDeclinationChange={(value) => form.setData('magnetic_declination', value)}
                            referenceError={form.errors.orientation_reference}
                            declinationServerError={form.errors.magnetic_declination}
                        />

                        <div
                            data-testid="edit-project-crs"
                            className="px-3 py-2 rounded border text-[11px] leading-relaxed"
                            style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)', color: 'var(--fg-3)' }}
                        >
                            <span className="font-mono uppercase tracking-wider">Coordinate system</span>{' '}
                            <span style={{ color: 'var(--fg-1)' }}>
                                {project.crs_epsg
                                    ? `EPSG:${project.crs_epsg}`
                                    : 'not set (CSV imports default to EPSG:32613)'}
                            </span>
                            <br />
                            Not editable here: changing it would not reproject the drill holes already ingested, leaving
                            the project in two coordinate systems.
                        </div>

                        {failure && (
                            <div
                                role="alert"
                                data-testid="edit-project-failure"
                                className="px-3 py-2 text-xs rounded border border-red-800/50 bg-red-950/40 text-red-300"
                            >
                                Failed to update project: {failure}
                            </div>
                        )}
                    </div>

                    <SheetFooter className="flex-row justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => onOpenChange(false)}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border"
                            style={{ color: 'var(--fg-2)', background: 'var(--bg-2)', borderColor: 'var(--line-2)' }}
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={form.processing || localDeclinationError !== undefined}
                            className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded border disabled:opacity-50"
                            style={{
                                color: 'var(--accent)',
                                background: 'var(--accent-bg)',
                                borderColor: 'var(--accent-dim)',
                            }}
                        >
                            {form.processing ? 'Saving…' : 'Save changes'}
                        </button>
                    </SheetFooter>
                </form>
            </SheetContent>
        </Sheet>
    );
}

function FieldError({ message }: { message?: string }) {
    if (!message) return null;
    return (
        <p role="alert" className="mt-1 text-[11px]" style={{ color: 'var(--danger, #f87171)' }}>
            {message}
        </p>
    );
}
