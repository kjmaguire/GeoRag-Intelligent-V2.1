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

export const inputStyle = { background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' } as CSSProperties;

export function Field({ label, required, children }: { label: string; required?: boolean; children: ReactNode }) {
    return (
        <label className="block">
            <span className="text-[10px] font-mono uppercase tracking-wider mb-1 block" style={{ color: 'var(--fg-3)' }}>
                {label}{required && <span style={{ color: 'var(--accent)' }}> *</span>}
            </span>
            {children}
        </label>
    );
}
