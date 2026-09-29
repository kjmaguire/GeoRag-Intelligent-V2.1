import { describe, expect, it } from 'vitest';
import { safeReturnTo } from '@/lib/safeReturnTo';

const ORIGIN = 'https://georag.example.com';

describe('safeReturnTo (FE-21)', () => {
    it.each([
        ['/projects/red-star/workspace?mode=3d', '/projects/red-star/workspace?mode=3d'],
        ['/projects', '/projects'],
        ['/projects/a#logs', '/projects/a#logs'],
    ])('accepts same-origin path %s', (raw, expected) => {
        expect(safeReturnTo(raw, ORIGIN)).toBe(expected);
    });

    it.each([
        '/\\evil.example',
        '/\\/evil.example',
        '//evil.example',
        'https://evil.example/',
        'javascript:alert(1)',
        '/\t/evil.example',
        '/login',
        '/login?return_to=/projects',
        '',
        null,
    ])('rejects %s', (raw) => {
        expect(safeReturnTo(raw as string | null, ORIGIN)).toBeNull();
    });

    it('the rejected backslash form really does escape the origin in a browser', () => {
        // Documents why the check exists: WHATWG parsing of the old accepted input.
        expect(new URL('/\\evil.example', ORIGIN).origin).toBe('https://evil.example');
    });
});
