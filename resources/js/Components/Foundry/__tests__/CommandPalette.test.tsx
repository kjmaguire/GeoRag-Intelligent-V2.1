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

describe('Foundry/CommandPalette keyboard behaviour', () => {
    it('hides the decorative kind label from assistive tech', () => {
        openPalette();
        const option = screen.getAllByRole('option')[0];
        const kind = option.querySelector('span');
        expect(kind).toHaveTextContent('nav');
        expect(kind).toHaveAttribute('aria-hidden', 'true');
    });

    it('starts on the first row every time it opens, including after picking from a lower row', () => {
        openPalette();
        const input = screen.getByRole('combobox');
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(screen.getAllByRole('option')[2]).toHaveAttribute('aria-selected', 'true');

        fireEvent.keyDown(input, { key: 'Enter' });
        expect(screen.queryByRole('dialog')).toBeNull();

        fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
        expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true');
        expect(screen.getByRole('combobox')).toHaveAttribute('aria-activedescendant', screen.getAllByRole('option')[0].id);
    });

    it('also starts on the first row after closing with Escape from a lower row', () => {
        openPalette();
        fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' });
        fireEvent.keyDown(window, { key: 'Escape' });
        fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
        expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true');
    });

    it('stops ArrowDown at the last row and never points at a missing option', () => {
        openPalette();
        const input = screen.getByRole('combobox');
        const count = screen.getAllByRole('option').length;
        for (let i = 0; i < count + 3; i++) fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(screen.getAllByRole('option')[count - 1]).toHaveAttribute('aria-selected', 'true');

        fireEvent.change(input, { target: { value: 'zzz-no-such-entry' } });
        expect(screen.queryAllByRole('option')).toHaveLength(0);
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(input).not.toHaveAttribute('aria-activedescendant');
        fireEvent.change(input, { target: { value: '' } });
        expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true');
    });

    it('keeps Tab inside the dialog', () => {
        openPalette();
        const input = screen.getByRole('combobox');
        expect(input).toHaveFocus();
        // fireEvent returns false when the default action was prevented.
        expect(fireEvent.keyDown(input, { key: 'Tab' })).toBe(false);
        expect(fireEvent.keyDown(input, { key: 'Tab', shiftKey: true })).toBe(false);
        expect(input).toHaveFocus();
    });

    it('returns focus to the element that opened it', () => {
        render(
            <>
                <button type="button">opener</button>
                <CommandPalette projectSlug="red-star" />
            </>,
        );
        const opener = screen.getByRole('button', { name: 'opener' });
        opener.focus();
        fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
        expect(screen.getByRole('combobox')).toHaveFocus();

        fireEvent.keyDown(window, { key: 'Escape' });
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(opener).toHaveFocus();
    });
});
