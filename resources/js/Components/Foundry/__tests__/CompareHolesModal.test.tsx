/**
 * FE-24 — the compare modal is a real dialog: named, modal, Escape closes it,
 * and focus moves inside it.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CompareHolesModal } from '../CompareHolesModal';

afterEach(() => {
    vi.restoreAllMocks();
});

describe('CompareHolesModal', () => {
    it('has dialog semantics, closes on Escape and takes focus', async () => {
        vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise(() => {}));
        const onClose = vi.fn();
        render(<CompareHolesModal projectSlug="p" leftHole="A-1" rightHole="B-2" onClose={onClose} />);

        const dialog = screen.getByRole('dialog', { name: 'Hole comparison: A-1 vs B-2' });
        await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

        fireEvent.keyDown(dialog, { key: 'Escape' });
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});
