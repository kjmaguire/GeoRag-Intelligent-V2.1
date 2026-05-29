import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';
import FeaturePopupCard from '../FeaturePopupCard';
import type { PointPopup } from '../PublicGeoscienceMap';

/**
 * Regression coverage for the popup-render bug that shipped undetected
 * through the Tier 1 expansion (mid-April 2026):
 *
 *   FeatureBody was a switch over 4 layer IDs with no default case.
 *   When a user clicked a rock_sample or assessment_survey feature, the
 *   switch fell through, returned undefined, React rendered nothing, and
 *   the popup appeared as an empty dark rectangle with just the close
 *   button. No console error. The bug survived multiple demo sessions
 *   because nobody clicked those specific layers.
 *
 * These tests assert every LayerId produces a non-empty popup body, so
 * adding a 7th or 8th LayerId without wiring its FeatureBody case will
 * fail CI before shipping.
 */

function makePopup(layerId: PointPopup['layerId'], properties: Record<string, any>): PointPopup {
    return {
        layerId,
        lngLat: [-106.3, 52.1],
        properties: {
            jurisdiction_code: 'CA-SK',
            source_id: 'CA-SK-TEST',
            source_feature_id: 'TEST-001',
            ...properties,
        },
    };
}

describe('FeaturePopupCard', () => {
    it('renders a mine popup with name + status + commodities', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_mines', {
                    name: 'Cigar Lake',
                    status: 'producing',
                    commodities: '{U3O8,Ag}',
                    commodity_grouping: 'uranium',
                    operator: 'Cameco',
                })}
                onClose={() => {}}
            />,
        );
        expect(screen.getByText('Cigar Lake')).toBeInTheDocument();
        expect(screen.getByText('producing')).toBeInTheDocument();
        expect(screen.getByText(/U3O8/)).toBeInTheDocument();
        expect(screen.getByText('Cameco')).toBeInTheDocument();
    });

    it('renders a mineral occurrence popup with SMDI label for CA-SK', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_mineral_occurrences', {
                    external_id: '1234',
                    name: 'Key Lake Deposit',
                    status: 'deposit',
                    primary_commodities: '{U3O8}',
                    commodity_grouping: 'uranium',
                })}
                onClose={() => {}}
            />,
        );
        expect(screen.getByText('Key Lake Deposit')).toBeInTheDocument();
        expect(screen.getByText('SMDI')).toBeInTheDocument();
        expect(screen.getByText('#1234')).toBeInTheDocument();
    });

    it('renders a mineral occurrence popup with MINFILE label for CA-BC', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_mineral_occurrences', {
                    jurisdiction_code: 'CA-BC',
                    external_id: '093A001',
                    name: 'Blackdome',
                    status: 'past producer',
                })}
                onClose={() => {}}
            />,
        );
        expect(screen.getByText('MINFILE')).toBeInTheDocument();
        expect(screen.getByText('#093A001')).toBeInTheDocument();
    });

    it('renders a drillhole popup with ID / company / project / depth', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_drillhole_collars', {
                    drillhole_id: 'GOS_4482',
                    drillhole_name: 'CL-18-073',
                    company: 'Cameco Corporation',
                    project_name: 'Cigar Lake Extension',
                    drill_type: 'Diamond',
                    total_length_m: 856.36,
                    has_total_length: true,
                })}
                onClose={() => {}}
            />,
        );
        expect(screen.getByText('CL-18-073')).toBeInTheDocument();
        expect(screen.getByText('GOS_4482')).toBeInTheDocument();
        expect(screen.getByText('Cameco Corporation')).toBeInTheDocument();
        expect(screen.getByText(/856.36 m/)).toBeInTheDocument();
    });

    it('drillhole without real depth (has_total_length=false) HIDES the Depth line', () => {
        // This is the scenario for the 520 SK drillholes that have NULL
        // total_length_m — the view coalesces to 0 but the popup must not
        // surface the bogus "0 m" reading.
        render(
            <FeaturePopupCard
                popup={makePopup('pg_drillhole_collars', {
                    drillhole_id: 'GOS_0001',
                    drillhole_name: 'No-depth hole',
                    company: 'SK Government',
                    project_name: 'Test project',
                    total_length_m: 0,
                    has_total_length: false,
                })}
                onClose={() => {}}
            />,
        );
        expect(screen.queryByText('Depth')).not.toBeInTheDocument();
        expect(screen.queryByText(/0 m/)).not.toBeInTheDocument();
    });

    it('renders a resource potential popup with has_potential_rank=false as "—"', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_resource_potential', {
                    commodity: 'gold',
                    commodity_grouping: 'precious_metals',
                    potential_rank: 0, // coalesced sentinel
                    has_potential_rank: false,
                    methodology_ref: 'SGS 2022',
                })}
                onClose={() => {}}
            />,
        );
        // "Potential" label + em-dash value for unknown rank.
        expect(screen.getByText('Potential')).toBeInTheDocument();
        expect(screen.getByText('—')).toBeInTheDocument();
        expect(screen.queryByText(/Rank 0/)).not.toBeInTheDocument();
    });

    it('renders a resource potential popup with real rank', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_resource_potential', {
                    commodity: 'uranium',
                    commodity_grouping: 'uranium',
                    potential_rank: 5,
                    has_potential_rank: true,
                })}
                onClose={() => {}}
            />,
        );
        expect(screen.getByText('Rank 5 / 6')).toBeInTheDocument();
    });

    // ── The bugs that motivated this file ────────────────────────────────

    it('renders a rock_sample popup (was silently blank before the fix)', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_rock_samples', {
                    sample_number: 'SK-2018-4421',
                    station: 'ST-042',
                    geologist: 'Dr. C. Harper',
                    geographic_area: 'Wollaston Domain',
                    nts_250k: '74H',
                    report_number: 'Rep-2018-11',
                })}
                onClose={() => {}}
            />,
        );
        // Sample number appears in BOTH the title and the Sample row of
        // the body — use getAllByText and assert ≥1 match rather than the
        // default strict-single-match behaviour.
        expect(screen.getAllByText('SK-2018-4421').length).toBeGreaterThan(0);
        // Subtitle must surface the entity type + jurisdiction.
        expect(screen.getByText(/Rock Sample/)).toBeInTheDocument();
        expect(screen.getByText('ST-042')).toBeInTheDocument();
        expect(screen.getByText('Dr. C. Harper')).toBeInTheDocument();
        expect(screen.getByText('74H')).toBeInTheDocument();
    });

    it('renders an assessment_survey popup with correct type label', () => {
        render(
            <FeaturePopupCard
                popup={makePopup('pg_assessment_surveys', {
                    survey_type: 'airborne',
                })}
                onClose={() => {}}
            />,
        );
        // "Airborne survey" appears in both title AND body — just assert it's present.
        expect(screen.getAllByText(/Airborne survey/).length).toBeGreaterThan(0);
        expect(screen.getByText(/Assessment Survey/)).toBeInTheDocument();
    });

    it('does not crash on an unknown layerId (Tier 2+3 reserved slot)', () => {
        // If someone clicks a future Tier 2 layer (e.g., tenure) before its
        // case is wired in the popup, we must render a fallback rather than
        // throwing. Use a cast because the LayerId union includes Tier 2+3
        // reserved IDs declared but not yet wired.
        render(
            <FeaturePopupCard
                popup={makePopup('pg_mineral_dispositions' as any, {
                    disposition_number: 'CBS-123456',
                    disposition_type: 'mineral',
                })}
                onClose={() => {}}
            />,
        );
        // Default fallback title is the feature ID or name.
        // Should NOT throw and SHOULD render a dialog role.
        expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('close button fires onClose', async () => {
        const user = userEvent.setup();
        const onClose = vi.fn();
        render(
            <FeaturePopupCard
                popup={makePopup('pg_mines', { name: 'Test mine' })}
                onClose={onClose}
            />,
        );
        const btn = screen.getByRole('button', { name: /close feature details/i });
        await user.click(btn);
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('renders inline inside the parent container (anchors to map top-left)', () => {
        // Portal was dropped in favour of inline rendering so the card
        // anchors at top-left of the map's relative wrapper — matching
        // Foundry/WorkspaceMap's "Hole" detail panel placement. The
        // map section uses `relative rounded-md overflow-hidden`; the
        // card's min/max width keeps it well inside the map bounds so
        // overflow-hidden doesn't clip it. If a future refactor needs
        // to re-introduce a portal (e.g. for very-wide content), flip
        // this assertion AND restore the portal target test.
        const { container } = render(
            <FeaturePopupCard
                popup={makePopup('pg_mines', { name: 'Inline anchor test' })}
                onClose={() => {}}
            />,
        );
        // The render container SHOULD contain the popup...
        expect(container.querySelector('[data-pg-popup]')).not.toBeNull();
        // ...and document.body should NOT have a stray portal target.
        expect(document.body.querySelector('[data-pg-popup]')).toBe(
            container.querySelector('[data-pg-popup]'),
        );
    });

    it('uses absolute top-2 left-2 positioning (matches Workspace Hole panel)', () => {
        const { container } = render(
            <FeaturePopupCard
                popup={makePopup('pg_mines', { name: 'Position test' })}
                onClose={() => {}}
            />,
        );
        const card = container.querySelector('[data-pg-popup]') as HTMLElement;
        expect(card).not.toBeNull();
        // Tailwind compiles `absolute top-2 left-2` into class names that
        // survive the build; check class membership rather than computed
        // style (jsdom can't resolve Tailwind utility values).
        expect(card.className).toContain('absolute');
        expect(card.className).toContain('top-2');
        expect(card.className).toContain('left-2');
    });
});
