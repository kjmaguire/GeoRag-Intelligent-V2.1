import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@inertiajs/react', () => ({ router: { visit: vi.fn() } }));

import CommandPalette from '../CommandPalette';

function openPalette() {
    render(<CommandPalette projectSlug="red-star" />);
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
}

afterEach(() => {
    vi.clearAllMocks();
});

describe('Foundry/CommandPalette accessibility', () => {
    it('exposes a modal dialog with a listbox and options', () => {
        openPalette();
        const dialog = screen.getByRole('dialog');
        expect(dialog).toHaveAttribute('aria-modal', 'true');
        expect(screen.getByRole('listbox')).toBeInTheDocument();
        expect(screen.getAllByRole('option').length).toBeGreaterThan(3);
    });

    it('moves aria-activedescendant and scrolls the active row into view', () => {
        const scroll = vi.fn();
        Element.prototype.scrollIntoView = scroll;
        openPalette();
        const input = screen.getByRole('combobox');
        const options = screen.getAllByRole('option');
        expect(input).toHaveAttribute('aria-activedescendant', options[0].id);
        expect(options[0]).toHaveAttribute('aria-selected', 'true');

        fireEvent.keyDown(input, { key: 'ArrowDown' });
        const after = screen.getAllByRole('option');
        expect(input).toHaveAttribute('aria-activedescendant', after[1].id);
        expect(after[1]).toHaveAttribute('aria-selected', 'true');
        expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });

        fireEvent.keyDown(input, { key: 'ArrowUp' });
        expect(input).toHaveAttribute('aria-activedescendant', screen.getAllByRole('option')[0].id);
    });
});
