import { describe, it, expect } from 'vitest';
import { cn } from '../utils';

describe('cn utility', () => {
    it('merges class names', () => {
        expect(cn('foo', 'bar')).toBe('foo bar');
    });

    it('handles conditional classes', () => {
        expect(cn('base', false && 'hidden', 'visible')).toBe('base visible');
    });

    it('resolves Tailwind conflicts (last wins)', () => {
        expect(cn('p-4', 'p-2')).toBe('p-2');
    });

    it('handles undefined and null inputs', () => {
        expect(cn('base', undefined, null, 'end')).toBe('base end');
    });

    it('handles empty string', () => {
        expect(cn('')).toBe('');
    });

    it('merges complex Tailwind classes', () => {
        expect(cn('text-red-500', 'text-blue-500')).toBe('text-blue-500');
    });

    it('keeps non-conflicting classes', () => {
        expect(cn('bg-red-500', 'text-white', 'p-4')).toBe('bg-red-500 text-white p-4');
    });
});
