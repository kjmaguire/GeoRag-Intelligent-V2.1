import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { LogCurveToggles, type AvailableLogCurve } from '../Foundry/LogCurveToggles';

const available: AvailableLogCurve[] = [
    { curve_name: 'GR', unit: 'API', group: 'gamma', sample_count: 100 },
    { curve_name: 'RESIST', unit: null, group: 'resistivity', sample_count: 100 },
    { curve_name: 'ZZ_CUSTOM', unit: null, group: 'other', sample_count: 100 },
];

describe('LogCurveToggles', () => {
    it('lists every available curve, including ones that are not drawn', () => {
        render(<LogCurveToggles available={available} selected={['GR']} max={12} onChange={() => {}} />);
        expect(screen.getByRole('button', { name: /GR/ })).toBeTruthy();
        expect(screen.getByRole('button', { name: /RESIST/ })).toBeTruthy();
        expect(screen.getByRole('button', { name: /ZZ_CUSTOM/ })).toBeTruthy();
        expect(screen.getByText(/1 of 3 shown/)).toBeTruthy();
    });

    it('marks drawn curves as pressed', () => {
        render(<LogCurveToggles available={available} selected={['GR', 'RESIST']} max={12} onChange={() => {}} />);
        expect(screen.getByRole('button', { name: /GR/ }).getAttribute('aria-pressed')).toBe('true');
        expect(screen.getByRole('button', { name: /ZZ_CUSTOM/ }).getAttribute('aria-pressed')).toBe('false');
    });

    it('asks for a new selection when an undrawn curve is clicked', () => {
        const onChange = vi.fn();
        render(<LogCurveToggles available={available} selected={['GR']} max={12} onChange={onChange} />);
        fireEvent.click(screen.getByRole('button', { name: /ZZ_CUSTOM/ }));
        expect(onChange).toHaveBeenCalledWith(['GR', 'ZZ_CUSTOM']);
    });

    it('does not let the last drawn curve be turned off', () => {
        const onChange = vi.fn();
        render(<LogCurveToggles available={available} selected={['GR']} max={12} onChange={onChange} />);
        fireEvent.click(screen.getByRole('button', { name: /GR/ }));
        expect(onChange).not.toHaveBeenCalled();
    });

    it('disables undrawn curves once the payload bound is reached', () => {
        render(<LogCurveToggles available={available} selected={['GR', 'RESIST']} max={2} onChange={() => {}} />);
        expect((screen.getByRole('button', { name: /ZZ_CUSTOM/ }) as HTMLButtonElement).disabled).toBe(true);
    });

    it('renders nothing when the hole has no curves', () => {
        const { container } = render(<LogCurveToggles available={[]} selected={[]} max={12} onChange={() => {}} />);
        expect(container.firstChild).toBeNull();
    });
});
