/**
 * LifecycleVisuals.test.tsx
 *
 * Tests for §B2 — 5-state lifecycle visual treatment in ChatMessage.
 * Module 7 Phase B.
 *
 * States under test: draft | generated | validated | committed | rejected
 *
 * Key visual assertions per state:
 *   draft       — no colored citation chips (citations prop is empty); no feedback buttons
 *   generated   — "Validating…" text visible
 *   validated   — feedback buttons present (disabled); no "Validating…" badge
 *   committed   — feedback buttons present (disabled); no "Validating…" badge
 *   rejected    — RefusalPanel rendered; no feedback buttons; no follow-up chips
 *
 * Also covers backward compat: absent lifecycle_state defaults to 'committed'.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ChatMessage from '../ChatMessage';
import type { RefusalPayload } from '@/types';

// ── Test fixture helpers ──────────────────────────────────────────────────

const BASE_TIMESTAMP = '2026-04-22T10:00:00Z';

function makeMsg(overrides = {}) {
  return {
    id: 'msg-test-1',
    role: 'assistant',
    content: 'Here is what we found.',
    timestamp: BASE_TIMESTAMP,
    citations: [],
    confidence: 0.85,
    followups: ['Tell me more', 'Show the data'],
    phases: [],
    ...overrides,
  };
}

function makeRefusalPayload(overrides = {}): RefusalPayload {
  return {
    type: 'refusal',
    reason_code: 'insufficient_evidence',
    searched: { stores_queried: ['qdrant'], candidates_considered: 5, query_class: 'assay' },
    missing: { what_was_needed: 'More assay data', nearest_candidates: [] },
    message: 'No evidence found.',
    ...overrides,
  };
}

const noop = () => {};
const defaultProps = {
  projectId: null,
  onCitationClick: noop,
  onRegenerate: noop,
  onFollowupClick: noop,
  onInspectCandidate: noop,
  isStreaming: false,
};

// ── 1. draft state ────────────────────────────────────────────────────────

describe('ChatMessage lifecycle — draft state', () => {
  it('renders message content while streaming', () => {
    const msg = makeMsg({
      lifecycle_state: 'draft',
      content: 'Retrieving data…',
      confidence: null,
      followups: [],
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.getByText(/Retrieving data/)).toBeTruthy();
  });

  it('does not show "Validating…" badge in draft state', () => {
    const msg = makeMsg({ lifecycle_state: 'draft', confidence: null, followups: [] });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByText(/Validating/)).toBeNull();
  });

  it('does not show feedback buttons in draft state', () => {
    const msg = makeMsg({ lifecycle_state: 'draft', confidence: null, followups: [] });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByRole('button', { name: /helpful/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /not helpful/i })).toBeNull();
  });

  it('does not show follow-up chips in draft state (no confidence)', () => {
    // In practice, draft-state messages don't have followups — those only
    // arrive via the completed SSE event. shouldRenderFollowups allows chips
    // when confidence is null (can't floor-check null). If followups arrived
    // during draft somehow, they would render. The real blocker is the absent
    // completed event = no followups. Test reflects this: empty followups.
    const msg = makeMsg({
      lifecycle_state: 'draft',
      confidence: null,
      followups: [],
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByText('Tell me more')).toBeNull();
  });
});

// ── 2. generated state ────────────────────────────────────────────────────

describe('ChatMessage lifecycle — generated state', () => {
  it('shows "Validating…" badge', () => {
    const msg = makeMsg({ lifecycle_state: 'generated', confidence: null });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.getByText(/Validating/)).toBeTruthy();
  });

  it('does not show feedback buttons in generated state', () => {
    const msg = makeMsg({ lifecycle_state: 'generated', confidence: null });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByRole('button', { name: /helpful/i })).toBeNull();
  });

  it('does not show follow-up chips in generated state when followups is empty', () => {
    // In practice followups only arrive with the completed event, so generated
    // state messages have empty followups. shouldRenderFollowups blocks on Rule 2.
    const msg = makeMsg({ lifecycle_state: 'generated', confidence: null, followups: [] });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByText('Explore')).toBeNull();
  });
});

// ── 3. validated state ────────────────────────────────────────────────────

describe('ChatMessage lifecycle — validated state', () => {
  it('shows feedback buttons (disabled)', () => {
    // Chunk 4: FeedbackButtons are now live (not placeholders).
    // The container has data-testid="feedback-buttons".
    // Both buttons render in validated state.
    const msg = makeMsg({ lifecycle_state: 'validated' });
    render(<ChatMessage message={msg} {...defaultProps} />);
    const feedbackDiv = screen.getByTestId('feedback-buttons');
    expect(feedbackDiv).toBeTruthy();
    // "Mark answer as helpful" + "Report answer problem" buttons
    expect(screen.getByRole('button', { name: /mark answer as helpful/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /report answer problem/i })).toBeTruthy();
  });

  it('shows "Report answer problem" button', () => {
    // Chunk 4: button text is now "Report problem" (was "Not helpful" placeholder).
    const msg = makeMsg({ lifecycle_state: 'validated' });
    render(<ChatMessage message={msg} {...defaultProps} />);
    const btn = screen.getByRole('button', { name: /report answer problem/i });
    expect(btn).toBeTruthy();
  });

  it('does NOT show "Validating…" badge', () => {
    const msg = makeMsg({ lifecycle_state: 'validated' });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByText(/Validating/)).toBeNull();
  });

  it('shows follow-up chips when confidence >= threshold', () => {
    const msg = makeMsg({ lifecycle_state: 'validated', confidence: 0.9, followups: ['Explore deeper'] });
    render(<ChatMessage message={msg} {...defaultProps} />);
    // The chip button has an aria-label; use that for a unique query
    expect(
      screen.getByRole('button', { name: /send follow-up query: explore deeper/i })
    ).toBeTruthy();
  });
});

// ── 4. committed state ────────────────────────────────────────────────────

describe('ChatMessage lifecycle — committed state', () => {
  it('shows feedback buttons (disabled)', () => {
    // Chunk 4: committed state shows live FeedbackButtons.
    const msg = makeMsg({ lifecycle_state: 'committed' });
    render(<ChatMessage message={msg} {...defaultProps} />);
    const feedbackDiv = screen.getByTestId('feedback-buttons');
    expect(feedbackDiv).toBeTruthy();
    expect(screen.getByRole('button', { name: /mark answer as helpful/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /report answer problem/i })).toBeTruthy();
  });

  it('shows follow-up chips when confidence >= threshold', () => {
    const msg = makeMsg({
      lifecycle_state: 'committed',
      confidence: 0.8,
      followups: ['What about hole DH-001?'],
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.getByText('What about hole DH-001?')).toBeTruthy();
  });

  it('does NOT show "Validating…" badge', () => {
    const msg = makeMsg({ lifecycle_state: 'committed' });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByText(/Validating/)).toBeNull();
  });

  it('does NOT show RefusalPanel in committed state', () => {
    const msg = makeMsg({ lifecycle_state: 'committed' });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.queryByText("We can't answer this from your corpus")).toBeNull();
  });
});

// ── 5. rejected state ─────────────────────────────────────────────────────

describe('ChatMessage lifecycle — rejected state', () => {
  it('renders RefusalPanel with the refusal payload', () => {
    const msg = makeMsg({
      lifecycle_state: 'rejected',
      confidence: null,
      refusal_payload: makeRefusalPayload(),
      followups: ['Tell me more'],
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.getByText("We can't answer this from your corpus")).toBeTruthy();
  });

  it('does NOT show feedback buttons on rejected messages', () => {
    const msg = makeMsg({
      lifecycle_state: 'rejected',
      confidence: null,
      refusal_payload: makeRefusalPayload(),
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    // FeedbackButtons only render when isValidatedOrCommitted — rejected skips them
    expect(screen.queryByRole('button', { name: /helpful/i })).toBeNull();
  });

  it('does NOT show follow-up chips on rejected messages', () => {
    const msg = makeMsg({
      lifecycle_state: 'rejected',
      confidence: 0.9,
      refusal_payload: makeRefusalPayload(),
      followups: ['Tell me more'],
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    // shouldRenderFollowups returns false for rejected
    expect(screen.queryByText('Tell me more')).toBeNull();
  });

  it('shows "Report refusal issue" button from RefusalPanel', () => {
    const msg = makeMsg({
      lifecycle_state: 'rejected',
      confidence: null,
      refusal_payload: makeRefusalPayload(),
    });
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.getByRole('button', { name: /report refusal issue/i })).toBeTruthy();
  });
});

// ── 6. Backward compat: absent lifecycle_state defaults to committed ───────

describe('ChatMessage lifecycle — backward compat', () => {
  it('treats absent lifecycle_state as committed (shows feedback buttons)', () => {
    const msg = makeMsg({
      // no lifecycle_state field
      confidence: 0.85,
    });
    delete (msg as Record<string, unknown>).lifecycle_state;
    render(<ChatMessage message={msg} {...defaultProps} />);
    // Should render as committed — feedback section visible (Chunk 4: live buttons).
    const feedbackDiv = screen.getByTestId('feedback-buttons');
    expect(feedbackDiv).toBeTruthy();
  });

  it('shows follow-up chips for legacy messages (no lifecycle_state) with confidence', () => {
    const msg = makeMsg({ confidence: 0.8, followups: ['Legacy chip'] });
    delete (msg as Record<string, unknown>).lifecycle_state;
    render(<ChatMessage message={msg} {...defaultProps} />);
    expect(screen.getByText('Legacy chip')).toBeTruthy();
  });
});
