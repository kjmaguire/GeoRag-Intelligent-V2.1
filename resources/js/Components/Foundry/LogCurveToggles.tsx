import { toggleCurveSelection } from '@/lib/workspaceLimits';

export interface AvailableLogCurve {
    curve_name: string;
    unit: string | null;
    group: string;
    sample_count: number;
}

/**
 * Chip row listing EVERY curve the active hole has, with the drawn ones
 * highlighted. Clicking a chip asks the parent for a new selection; the
 * parent re-requests the page with `log_curves=` (the server downsamples and
 * sends only the selected tracks, so the payload stays bounded no matter how
 * many curves the hole carries).
 */
export function LogCurveToggles({
    available,
    selected,
    max,
    onChange,
}: {
    available: AvailableLogCurve[];
    selected: string[];
    max: number;
    onChange: (next: string[]) => void;
}) {
    if (available.length === 0) return null;
    const atMax = selected.length >= max;

    return (
        <div className="mb-3 shrink-0" data-testid="log-curve-toggles">
            <div className="text-[10px] font-mono uppercase tracking-wider mb-1" style={{ color: 'var(--fg-3)' }}>
                Curves · {selected.length} of {available.length} shown
                {atMax && available.length > max ? ` (max ${max})` : ''}
            </div>
            <div className="flex flex-wrap gap-1.5">
                {available.map((c) => {
                    const on = selected.includes(c.curve_name);
                    const blocked = !on && atMax;
                    return (
                        <button
                            key={c.curve_name}
                            type="button"
                            aria-pressed={on}
                            disabled={blocked}
                            title={
                                c.unit
                                    ? `${c.curve_name} (${c.unit}) · ${c.sample_count} samples`
                                    : `${c.curve_name} · ${c.sample_count} samples`
                            }
                            onClick={() => {
                                const next = toggleCurveSelection(selected, c.curve_name, max);
                                if (next) onChange(next);
                            }}
                            className="text-[11px] font-mono px-2 py-0.5 rounded border disabled:opacity-40"
                            style={{
                                borderColor: on ? 'var(--line-3, var(--fg-2))' : 'var(--line-2)',
                                color: on ? 'var(--fg-0)' : 'var(--fg-3)',
                                background: on ? 'var(--bg-3, var(--bg-2))' : 'var(--bg-2)',
                            }}
                        >
                            {c.curve_name}
                            {c.unit ? <span style={{ color: 'var(--fg-3)' }}> {c.unit}</span> : null}
                        </button>
                    );
                })}
            </div>
        </div>
    );
}
