import { describe, expect, it } from 'vitest';
import { describeTruncation, toggleCurveSelection, type WorkspaceTruncation } from '../workspaceLimits';

const none: WorkspaceTruncation = {
    collars: { shown: 2, total: 2, truncated: false },
    interval_holes: { shown: 2, total: 2, truncated: false },
    survey_holes_downsampled: 0,
};

describe('describeTruncation', () => {
    it('is silent when nothing was capped or the prop is absent', () => {
        expect(describeTruncation(none)).toEqual([]);
        expect(describeTruncation(undefined)).toEqual([]);
        expect(describeTruncation(null)).toEqual([]);
    });

    it('says "showing N of M holes" when the collar cap bites', () => {
        const notices = describeTruncation({
            ...none,
            collars: { shown: 1000, total: 1432, truncated: true },
            interval_holes: { shown: 200, total: 1432, truncated: true },
        });
        expect(notices[0]).toBe('Showing 1,000 of 1,432 holes on the map and in 3D.');
        expect(notices[1]).toBe('3D lithology shows 200 of 1,432 holes.');
    });

    it('reports the interval cap on its own when the collar cap did not bite', () => {
        const notices = describeTruncation({
            ...none,
            collars: { shown: 300, total: 300, truncated: false },
            interval_holes: { shown: 200, total: 300, truncated: true },
        });
        expect(notices).toEqual(['3D lithology shows 200 of 300 holes.']);
    });

    it('reports thinned survey stations', () => {
        expect(describeTruncation({ ...none, survey_holes_downsampled: 3 })).toEqual([
            'Survey stations thinned for 3 holes (first and last kept).',
        ]);
    });
});

describe('toggleCurveSelection', () => {
    it('adds a curve that is off', () => {
        expect(toggleCurveSelection(['GR'], 'RT', 4)).toEqual(['GR', 'RT']);
    });

    it('removes a curve that is on', () => {
        expect(toggleCurveSelection(['GR', 'RT'], 'GR', 4)).toEqual(['RT']);
    });

    it('refuses to turn off the last curve', () => {
        expect(toggleCurveSelection(['GR'], 'GR', 4)).toBeNull();
    });

    it('refuses to exceed the payload bound', () => {
        expect(toggleCurveSelection(['A', 'B'], 'C', 2)).toBeNull();
    });
});
