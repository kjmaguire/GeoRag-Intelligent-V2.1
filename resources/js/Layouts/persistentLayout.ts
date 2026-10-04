import AppLayout from '@/Layouts/AppLayout';

/**
 * Default (persistent) layout for Inertia pages — FE-13, 2026-09-29.
 *
 * Every Foundry page used to render `<AppLayout>` inside itself, so the shell
 * unmounted and remounted on every navigation: ProjectSelector refetched and
 * flashed "Loading projects…", toasts were dropped, the ingest-toast dedupe
 * set reset, and the shared `project.{id}.ingestion` channel hit refcount 0 →
 * Echo.leave → re-auth, losing events in the gap. Returning the SAME layout
 * component for consecutive pages lets React keep it mounted.
 *
 * Pages that still wrap themselves are listed here and get no default layout
 * (a second shell would nest inside the first):
 *   - Foundry/Chat — owned by the chat workstream; migrate by deleting its
 *     `<AppLayout>` wrapper and this entry together.
 *   - Foundry/DrillholeDetail — being edited by the gold-mineralization PR;
 *     same migration once that lands.
 * Auth and error pages (Login, ForgotPassword, ResetPassword, Error) have no
 * shell at all.
 */
export const SELF_WRAPPED_PAGES: ReadonlySet<string> = new Set(['Foundry/Chat', 'Foundry/DrillholeDetail']);

export function resolvePageLayout(name: string): typeof AppLayout | null {
    if (!name.startsWith('Foundry/')) return null;
    if (SELF_WRAPPED_PAGES.has(name)) return null;
    return AppLayout;
}
