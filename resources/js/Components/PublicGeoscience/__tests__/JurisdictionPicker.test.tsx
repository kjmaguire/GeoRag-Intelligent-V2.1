import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import type { Jurisdiction, CountryGroup } from '@/Types/PublicGeoscience';

// JurisdictionPicker uses radix-ui Tooltip which tries to render into a
// portal.  We keep the real implementation because it degrades gracefully
// in jsdom, but we suppress the missing ResizeObserver warning.
globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
};

import JurisdictionPicker from '../JurisdictionPicker';

// ── Fixtures ──────────────────────────────────────────────────────────────

const makeJ = (overrides: Partial<Jurisdiction> = {}): Jurisdiction => ({
    jurisdiction_code: 'CA-SK',
    country_code: 'CA',
    display_name: 'Saskatchewan',
    level: 'province',
    status: 'active',
    primary_authority: null,
    license_summary: null,
    license_url: null,
    default_source_crs: 2957,
    refresh_cadence: 'weekly',
    last_refreshed_at: null,
    teaser: null,
    sort_order: 10,
    bbox: null,
    sources: [],
    ...overrides,
});

function makeCanada(jurisdictions: Jurisdiction[] = [makeJ()]): CountryGroup {
    return {
        country_code: 'CA',
        display_name: 'Canada',
        jurisdictions,
    };
}

const defaultProps = {
    countries: [makeCanada()],
    selectedCode: null as string | null,
    onSelect: vi.fn(),
    loading: false,
    error: null as string | null,
    onRetry: vi.fn(),
};

function renderPicker(props: Partial<typeof defaultProps> = {}) {
    return render(<JurisdictionPicker {...defaultProps} {...props} />);
}

// ── Loading state ─────────────────────────────────────────────────────────

describe('JurisdictionPicker — loading state', () => {
    it('renders loading text when loading is true', () => {
        renderPicker({ loading: true, countries: [] });
        expect(screen.getByText(/Loading jurisdictions/i)).toBeTruthy();
    });

    it('does not render any country card when loading', () => {
        renderPicker({ loading: true });
        expect(screen.queryByText('Canada')).toBeNull();
    });
});

// ── Error state ───────────────────────────────────────────────────────────

describe('JurisdictionPicker — error state', () => {
    it('renders the error message when error prop is set', () => {
        renderPicker({ error: 'Fetch failed', countries: [] });
        expect(screen.getByText(/Fetch failed/)).toBeTruthy();
    });

    it('renders a Retry button', () => {
        renderPicker({ error: 'Fetch failed', countries: [] });
        expect(screen.getByRole('button', { name: /Retry/i })).toBeTruthy();
    });

    it('Retry button fires onRetry when clicked', () => {
        const onRetry = vi.fn();
        renderPicker({ error: 'Network error', countries: [], onRetry });
        fireEvent.click(screen.getByRole('button', { name: /Retry/i }));
        expect(onRetry).toHaveBeenCalledTimes(1);
    });

    it('does not render country cards when error is set', () => {
        renderPicker({ error: 'Oops', countries: [makeCanada()] });
        // Country card header should not be visible
        expect(screen.queryByText('Canada')).toBeNull();
    });
});

// ── Empty state ───────────────────────────────────────────────────────────

describe('JurisdictionPicker — empty state', () => {
    it('renders "No jurisdictions registered yet." when countries array is empty', () => {
        renderPicker({ countries: [] });
        expect(screen.getByText(/No jurisdictions registered yet\./i)).toBeTruthy();
    });
});

// ── Country card with jurisdictions ───────────────────────────────────────

describe('JurisdictionPicker — country card', () => {
    it('renders the country display name', () => {
        renderPicker();
        expect(screen.getByText('Canada')).toBeTruthy();
    });

    it('renders a toggle button for the country', () => {
        renderPicker();
        const toggle = screen.getByRole('button', { name: /Canada/ });
        expect(toggle).toBeTruthy();
    });

    it('Canada is expanded by default (aria-expanded=true)', () => {
        renderPicker();
        const toggle = screen.getByRole('button', { name: /Canada/ });
        expect(toggle).toHaveAttribute('aria-expanded', 'true');
    });

    it('shows jurisdiction tile when expanded', () => {
        renderPicker();
        expect(screen.getByText('Saskatchewan')).toBeTruthy();
    });

    it('collapses the country when the toggle is clicked', () => {
        renderPicker();
        const toggle = screen.getByRole('button', { name: /Canada/ });
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute('aria-expanded', 'false');
        // Saskatchewan tile should be hidden
        expect(screen.queryByText('Saskatchewan')).toBeNull();
    });

    it('re-expands the country on a second click', () => {
        renderPicker();
        const toggle = screen.getByRole('button', { name: /Canada/ });
        fireEvent.click(toggle);
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute('aria-expanded', 'true');
        expect(screen.getByText('Saskatchewan')).toBeTruthy();
    });

    it('shows the jurisdiction count in the header', () => {
        const twoJurisdictions = [
            makeJ({ jurisdiction_code: 'CA-SK', display_name: 'Saskatchewan' }),
            makeJ({ jurisdiction_code: 'CA-AB', display_name: 'Alberta' }),
        ];
        renderPicker({ countries: [makeCanada(twoJurisdictions)] });
        // The count "2" appears as a mono span next to the country name
        expect(screen.getByText('2')).toBeTruthy();
    });
});

// ── Active tile — click fires onSelect ────────────────────────────────────

describe('JurisdictionPicker — active tile', () => {
    it('clicking an active tile fires onSelect with the full Jurisdiction object', () => {
        const onSelect = vi.fn();
        const sk = makeJ({ status: 'active' });
        renderPicker({ countries: [makeCanada([sk])], onSelect });

        const tile = screen.getByRole('button', { name: /Saskatchewan/ });
        fireEvent.click(tile);

        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onSelect).toHaveBeenCalledWith(sk);
    });

    it('fires onSelect with the correct object when multiple jurisdictions exist', () => {
        const onSelect = vi.fn();
        const sk = makeJ({ jurisdiction_code: 'CA-SK', display_name: 'Saskatchewan', status: 'active' });
        const ab = makeJ({ jurisdiction_code: 'CA-AB', display_name: 'Alberta', status: 'active' });
        renderPicker({ countries: [makeCanada([sk, ab])], onSelect });

        fireEvent.click(screen.getByRole('button', { name: /Alberta/ }));
        expect(onSelect).toHaveBeenCalledWith(ab);
    });
});

// ── Coming-soon tile ──────────────────────────────────────────────────────

describe('JurisdictionPicker — coming_soon tile', () => {
    it('does not fire onSelect when a coming_soon tile is clicked', () => {
        const onSelect = vi.fn();
        const bc = makeJ({
            jurisdiction_code: 'CA-BC',
            display_name: 'British Columbia',
            status: 'coming_soon',
        });
        renderPicker({ countries: [makeCanada([bc])], onSelect });

        const tile = screen.getByRole('button', { name: /British Columbia/ });
        fireEvent.click(tile);

        expect(onSelect).not.toHaveBeenCalled();
    });

    it('coming_soon tile button is marked aria-disabled', () => {
        const bc = makeJ({ status: 'coming_soon', display_name: 'British Columbia' });
        renderPicker({ countries: [makeCanada([bc])] });

        const tile = screen.getByRole('button', { name: /British Columbia/ });
        expect(tile).toHaveAttribute('aria-disabled', 'true');
    });

    it('coming_soon tile has opacity-60 class', () => {
        const bc = makeJ({ status: 'coming_soon', display_name: 'British Columbia' });
        renderPicker({ countries: [makeCanada([bc])] });

        const tile = screen.getByRole('button', { name: /British Columbia/ });
        expect(tile.className).toContain('opacity-60');
    });

    it('coming_soon tile has cursor-not-allowed class', () => {
        const bc = makeJ({ status: 'coming_soon', display_name: 'British Columbia' });
        renderPicker({ countries: [makeCanada([bc])] });

        const tile = screen.getByRole('button', { name: /British Columbia/ });
        expect(tile.className).toContain('cursor-not-allowed');
    });

    it('renders the "Coming Soon" badge text', () => {
        const bc = makeJ({ status: 'coming_soon', display_name: 'British Columbia' });
        renderPicker({ countries: [makeCanada([bc])] });
        expect(screen.getByText('Coming Soon')).toBeTruthy();
    });
});

// ── Selected tile styling ─────────────────────────────────────────────────

describe('JurisdictionPicker — selected tile', () => {
    it('selected tile has ring and bg classes indicating selection', () => {
        const sk = makeJ({ status: 'active' });
        renderPicker({ selectedCode: 'CA-SK', countries: [makeCanada([sk])] });

        const tile = screen.getByRole('button', { name: /Saskatchewan/ });
        expect(tile.className).toContain('border-amber-500');
        expect(tile.className).toContain('ring-1');
        expect(tile.className).toContain('ring-amber-500');
    });

    it('unselected active tile does not have selection ring classes', () => {
        const sk = makeJ({ status: 'active' });
        renderPicker({ selectedCode: null, countries: [makeCanada([sk])] });

        const tile = screen.getByRole('button', { name: /Saskatchewan/ });
        // The tile carries `focus:ring-amber-500` (accessibility focus
        // affordance) but should NOT have `ring-1 ring-amber-500` (the
        // selected-state styling). Assert on the selected-state class.
        expect(tile.className).not.toContain('ring-1');
    });
});

// ── Teaser text ───────────────────────────────────────────────────────────

describe('JurisdictionPicker — teaser', () => {
    it('renders teaser text when provided', () => {
        const sk = makeJ({ teaser: 'Saskatchewan data coming Q3 2026' });
        renderPicker({ countries: [makeCanada([sk])] });
        expect(screen.getByText('Saskatchewan data coming Q3 2026')).toBeTruthy();
    });

    it('does not render teaser element when teaser is null', () => {
        const sk = makeJ({ teaser: null });
        renderPicker({ countries: [makeCanada([sk])] });
        // No extra text node from teaser
        expect(screen.queryByText(/coming Q/)).toBeNull();
    });
});
