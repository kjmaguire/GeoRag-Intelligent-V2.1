/**
 * Links into the upload wizard (/foundry/imports/wizard) from a project page.
 *
 * The wizard is global — it has its own project picker — so a link from a
 * project carries that project's slug as `?project=` and the wizard
 * preselects it. Without the parameter the wizard behaves as before: it
 * preselects only when the user has exactly one project.
 */
export const IMPORT_WIZARD_PATH = '/foundry/imports/wizard';

/** The wizard URL with `slug` preselected as the target project. */
export function importWizardHref(slug?: string | null): string {
    return slug ? `${IMPORT_WIZARD_PATH}?project=${encodeURIComponent(slug)}` : IMPORT_WIZARD_PATH;
}

/** The `?project=` slug (or project id) the wizard was opened with, if any. */
export function requestedProject(search: string): string | null {
    const value = new URLSearchParams(search).get('project');
    return value && value.trim() !== '' ? value.trim() : null;
}
