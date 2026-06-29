/**
 * RefusalPanel.test.tsx
 *
 * Tests for the structured refusal UX component (REF-01).
 * Module 7 Phase B §B7.
 *
 * Coverage:
 *   - All 6 reason_code header text mappings
 *   - NearestCandidateCard click invokes onInspectCandidate handler
 *   - failed_guards block conditional rendering
 *   - No follow-up chips rendered inside the panel
 *   - Report refusal issue button fires callback
 *   - System-level codes render Alert variant (llm_unavailable, budget_exhausted)
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { RefusalPanel } from '../chat/RefusalPanel';
import type { RefusalPayload } from '@/types';

// ── Helpers ──────────────────────────────────────────────────────────────

function makePayload(overrides: Partial<RefusalPayload> = {}): RefusalPayload {
  return {
    type: 'refusal',
    reason_code: 'insufficient_evidence',
    searched: {
      stores_queried: ['qdrant', 'neo4j'],
      candidates_considered: 42,
      query_class: 'assay_query',
    },
    missing: {
      what_was_needed: 'Assay data for hole DH-2547',
      nearest_candidates: [],
    },
    message: 'No sufficient evidence found.',
    ...overrides,
  };
}

const noop = () => {};

// ── 1. Header text per reason_code (6 tests) ──────────────────────────────

describe('RefusalPanel — header text by reason_code', () => {
  it('insufficient_evidence: shows correct header', () => {
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'insufficient_evidence' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(
      screen.getByText("We can't answer this from your corpus")
    ).toBeTruthy();
  });

  it('guard_numeric_fail: shows correct header', () => {
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'guard_numeric_fail' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(
      screen.getByText("Numbers in the draft answer don't check out")
    ).toBeTruthy();
  });

  it('guard_entity_fail: shows correct header', () => {
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'guard_entity_fail' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(
      screen.getByText("Entities in the draft answer don't match the evidence")
    ).toBeTruthy();
  });

  it('guard_completeness_fail: shows correct header', () => {
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'guard_completeness_fail' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(
      screen.getByText("Not every claim was supported by evidence")
    ).toBeTruthy();
  });

  it('llm_unavailable: shows correct header via Alert variant', () => {
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'llm_unavailable', message: 'Model offline.' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(
      screen.getByText("The language model is temporarily unavailable")
    ).toBeTruthy();
  });

  it('budget_exhausted: shows correct header via Alert variant', () => {
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'budget_exhausted', message: 'Timed out.' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(
      screen.getByText("The query exceeded its time budget")
    ).toBeTruthy();
  });
});

// ── 2. Nearest candidates click invokes handler ────────────────────────────

describe('RefusalPanel — nearest_candidates interaction', () => {
  it('clicking a candidate card calls onInspectCandidate with marker + evidenceId', () => {
    const handler = vi.fn();
    const payload = makePayload({
      missing: {
        what_was_needed: 'Grade data',
        nearest_candidates: [
          {
            marker: '[DATA:42]',
            source_store: 'qdrant',
            relevance_score: 0.71,
            preview: 'Gold intercept data for DH-2547…',
            evidence_id: 'ev-uuid-001',
          },
        ],
      },
    });
    render(
      <RefusalPanel
        payload={payload}
        onInspectCandidate={handler}
        onReportRefusalIssue={noop}
      />
    );

    const candidateBtn = screen.getByRole('button', {
      name: /inspect candidate.*\[DATA:42\]/i,
    });
    fireEvent.click(candidateBtn);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith('[DATA:42]', 'ev-uuid-001', null);
  });

  it('candidate without evidence_id passes null as second arg', () => {
    const handler = vi.fn();
    const payload = makePayload({
      missing: {
        what_was_needed: 'Grade data',
        nearest_candidates: [
          {
            marker: '[NI43:7]',
            source_store: 'neo4j',
            relevance_score: 0.55,
            preview: 'Section 14.3 resource estimate…',
          },
        ],
      },
    });
    render(
      <RefusalPanel
        payload={payload}
        onInspectCandidate={handler}
        onReportRefusalIssue={noop}
      />
    );
    const btn = screen.getByRole('button', { name: /inspect candidate/i });
    fireEvent.click(btn);
    expect(handler).toHaveBeenCalledWith('[NI43:7]', null, null);
  });

  it('shows relevance score formatted as percentage', () => {
    const payload = makePayload({
      missing: {
        what_was_needed: 'Assay data',
        nearest_candidates: [
          {
            marker: '[DATA:1]',
            source_store: 'qdrant',
            relevance_score: 0.88,
            preview: 'Uranium grade logging…',
          },
        ],
      },
    });
    render(
      <RefusalPanel payload={payload} onInspectCandidate={noop} onReportRefusalIssue={noop} />
    );
    expect(screen.getByText('88% relevance')).toBeTruthy();
  });

  it('truncates preview text longer than 160 chars', () => {
    const longPreview = 'A'.repeat(200);
    const payload = makePayload({
      missing: {
        what_was_needed: 'Something',
        nearest_candidates: [
          {
            marker: '[DATA:2]',
            source_store: 'qdrant',
            relevance_score: 0.5,
            preview: longPreview,
          },
        ],
      },
    });
    render(
      <RefusalPanel payload={payload} onInspectCandidate={noop} onReportRefusalIssue={noop} />
    );
    // Should show truncated text ending with ellipsis
    const truncated = screen.getByText(/A+…/);
    expect(truncated.textContent!.length).toBeLessThan(longPreview.length);
  });

  it('shows max 3 candidates even when more are provided', () => {
    const candidates = Array.from({ length: 5 }, (_, i) => ({
      marker: `[DATA:${i}]`,
      source_store: 'qdrant',
      relevance_score: 0.5 - i * 0.05,
      preview: `Candidate ${i}`,
    }));
    const payload = makePayload({
      missing: { what_was_needed: 'Data', nearest_candidates: candidates },
    });
    render(
      <RefusalPanel payload={payload} onInspectCandidate={noop} onReportRefusalIssue={noop} />
    );
    const cards = screen.getAllByRole('button', { name: /inspect candidate/i });
    expect(cards).toHaveLength(3);
  });
});

// ── 3. failed_guards conditional block ────────────────────────────────────

describe('RefusalPanel — failed_guards', () => {
  it('shows failed_guards section when guards are present', () => {
    const payload = makePayload({
      failed_guards: ['numeric_claim_guard', 'entity_resolution_guard'],
    });
    render(
      <RefusalPanel payload={payload} onInspectCandidate={noop} onReportRefusalIssue={noop} />
    );
    expect(screen.getByText('Hallucination guards triggered')).toBeTruthy();
    expect(screen.getByText('numeric_claim_guard')).toBeTruthy();
    expect(screen.getByText('entity_resolution_guard')).toBeTruthy();
  });

  it('does NOT show failed_guards section when absent', () => {
    const payload = makePayload({ failed_guards: undefined });
    render(
      <RefusalPanel payload={payload} onInspectCandidate={noop} onReportRefusalIssue={noop} />
    );
    expect(screen.queryByText('Hallucination guards triggered')).toBeNull();
  });

  it('does NOT show failed_guards section when empty array', () => {
    const payload = makePayload({ failed_guards: [] });
    render(
      <RefusalPanel payload={payload} onInspectCandidate={noop} onReportRefusalIssue={noop} />
    );
    expect(screen.queryByText('Hallucination guards triggered')).toBeNull();
  });
});

// ── 4. No follow-up chips rendered inside panel ────────────────────────────

describe('RefusalPanel — no follow-up chips', () => {
  it('renders no follow-up chip elements inside the panel', () => {
    render(
      <RefusalPanel
        payload={makePayload()}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    // RefusalPanel must not contain any "Explore deeper" label (the chip container label)
    expect(screen.queryByText('Explore deeper')).toBeNull();
  });
});

// ── 5. Report refusal issue button fires callback ──────────────────────────

describe('RefusalPanel — report refusal issue button', () => {
  it('calls onReportRefusalIssue when button is clicked (grounding refusal)', () => {
    const handler = vi.fn();
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'insufficient_evidence' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={handler}
      />
    );
    const btn = screen.getByRole('button', { name: /report refusal issue/i });
    fireEvent.click(btn);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('calls onReportRefusalIssue when button is clicked (system-level refusal)', () => {
    const handler = vi.fn();
    render(
      <RefusalPanel
        payload={makePayload({ reason_code: 'llm_unavailable' })}
        onInspectCandidate={noop}
        onReportRefusalIssue={handler}
      />
    );
    const btn = screen.getByRole('button', { name: /report refusal issue/i });
    fireEvent.click(btn);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

// ── 6. Searched block content ──────────────────────────────────────────────

describe('RefusalPanel — searched block', () => {
  it('shows candidate count and store names', () => {
    render(
      <RefusalPanel
        payload={makePayload({
          searched: {
            stores_queried: ['qdrant', 'postgis'],
            candidates_considered: 77,
            query_class: 'drill_summary',
          },
        })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(screen.getByText(/77/)).toBeTruthy();
    expect(screen.getByText(/Qdrant/)).toBeTruthy();
    expect(screen.getByText(/PostGIS/)).toBeTruthy();
  });

  it('shows query class badge', () => {
    render(
      <RefusalPanel
        payload={makePayload({
          searched: {
            stores_queried: ['qdrant'],
            candidates_considered: 10,
            query_class: 'geochemistry',
          },
        })}
        onInspectCandidate={noop}
        onReportRefusalIssue={noop}
      />
    );
    expect(screen.getByText('geochemistry')).toBeTruthy();
  });
});
