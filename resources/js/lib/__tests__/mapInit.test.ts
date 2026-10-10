import { describe, expect, it } from 'vitest';
import { mapStartFailure } from '../mapInit';

describe('mapStartFailure', () => {
    it('names WebGL 2 when that is what failed', () => {
        expect(mapStartFailure(new Error('Failed to initialize WebGL'))).toMatch(/needs WebGL 2/);
    });

    it('asks for a reload when the map chunk did not load', () => {
        expect(
            mapStartFailure(new TypeError('Failed to fetch dynamically imported module: /build/assets/x.js')),
        ).toMatch(/could not be loaded\. Reload/);
    });

    it('says the rest of the page still works for anything else', () => {
        expect(mapStartFailure('boom')).toMatch(/could not start\. The rest of the page still works/);
    });
});
