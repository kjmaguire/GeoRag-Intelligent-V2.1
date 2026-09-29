/**
 * "+ Add Documents" on a project page opens the global upload wizard with that
 * project preselected. Before 2026-09-29 no project page linked to the wizard
 * with the project attached, so adding documents to an existing project meant
 * finding the wizard and re-picking the project by hand.
 */
import { describe, it, expect } from 'vitest';
import { IMPORT_WIZARD_PATH, importWizardHref, requestedProject } from '../importWizardLink';

describe('importWizardHref', () => {
    it('carries the project slug', () => {
        expect(importWizardHref('waffles')).toBe('/foundry/imports/wizard?project=waffles');
    });

    it('encodes slugs that are not URL-safe', () => {
        expect(importWizardHref('red lake&co')).toBe('/foundry/imports/wizard?project=red%20lake%26co');
    });

    it('falls back to the bare wizard without a slug', () => {
        expect(importWizardHref(null)).toBe(IMPORT_WIZARD_PATH);
        expect(importWizardHref('')).toBe(IMPORT_WIZARD_PATH);
    });
});

describe('requestedProject', () => {
    it('round-trips what importWizardHref writes', () => {
        const href = importWizardHref('red lake&co');
        expect(requestedProject(href.slice(href.indexOf('?')))).toBe('red lake&co');
    });

    it('is null when absent or blank', () => {
        expect(requestedProject('')).toBeNull();
        expect(requestedProject('?other=1')).toBeNull();
        expect(requestedProject('?project=%20%20')).toBeNull();
    });
});
