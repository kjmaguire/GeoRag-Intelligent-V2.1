// @ts-nocheck
/**
 * FollowupOmit.test.tsx
 *
 * Tests for shouldRenderFollowups() — Module 7 Phase B §B5 omit rules.
 *
 * 4 omit rules:
 *   Rule 1: rejected messages never show chips
 *   Rule 2: no chips when followups is empty / missing
 *   Rule 3: confidence floor < 0.25 → chips are unreliable noise
 *   Rule 4: workspace preference (always passes — deferred until workspace_settings ships)
 *
 * Happy path: non-rejected, has followups, confidence >= 0.25
 */
import { describe, it, expect } from 'vitest';
import { shouldRenderFollowups } from '../ChatMessage';

// ── Rule 1: rejected state ─────────────────────────────────────────────────

describe('shouldRenderFollowups — Rule 1: rejected state', () => {
  it('returns false when lifecycle_state is "rejected"', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'rejected',
        followups: ['Tell me more'],
        confidence: 0.9,
      })
    ).toBe(false);
  });

  it('returns false for rejected even with high confidence and many followups', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'rejected',
        followups: ['A', 'B', 'C'],
        confidence: 0.99,
      })
    ).toBe(false);
  });
});

// ── Rule 2: empty followups ────────────────────────────────────────────────

describe('shouldRenderFollowups — Rule 2: empty or missing followups', () => {
  it('returns false when followups is empty array', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: [],
        confidence: 0.9,
      })
    ).toBe(false);
  });

  it('returns false when followups is undefined', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: undefined,
        confidence: 0.9,
      })
    ).toBe(false);
  });

  it('returns false when followups is null', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: null as any,
        confidence: 0.9,
      })
    ).toBe(false);
  });
});

// ── Rule 3: confidence floor ───────────────────────────────────────────────

describe('shouldRenderFollowups — Rule 3: confidence floor', () => {
  it('returns false when confidence is exactly 0.0', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: ['Explore'],
        confidence: 0.0,
      })
    ).toBe(false);
  });

  it('returns false when confidence is 0.24 (below threshold 0.25)', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: ['Explore'],
        confidence: 0.24,
      })
    ).toBe(false);
  });

  it('returns true when confidence is exactly 0.25 (at threshold)', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: ['Explore'],
        confidence: 0.25,
      })
    ).toBe(true);
  });

  it('returns true when confidence is 0.5 (above threshold)', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: ['Explore'],
        confidence: 0.5,
      })
    ).toBe(true);
  });

  it('passes when confidence is null (cannot floor-check null)', () => {
    // Null confidence means the answer is still completing — not a low-confidence signal.
    // Rule 3 only fires when confidence != null AND < threshold.
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'validated',
        followups: ['Explore'],
        confidence: null,
      })
    ).toBe(true);
  });
});

// ── Rule 4: workspace preference (always passes — deferred to workspace_settings) ──

describe('shouldRenderFollowups — Rule 4: workspace preference (passthrough)', () => {
  it('returns true without workspace_settings (deferred until workspace_settings ships)', () => {
    // Rule 4 always passes. workspace_settings.followup_chips_enabled is deferred;
    // see ops/backlog/v1.5-followups.md. Do not block chips until that ships.
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: ['Next question'],
        confidence: 0.8,
      })
    ).toBe(true);
  });
});

// ── Happy path ─────────────────────────────────────────────────────────────

describe('shouldRenderFollowups — happy path', () => {
  it('returns true for validated state with chips and sufficient confidence', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'validated',
        followups: ['Show me the grade table', 'Compare across holes'],
        confidence: 0.85,
      })
    ).toBe(true);
  });

  it('returns true for committed state (standard post-answer state)', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'committed',
        followups: ['What is the mean Au grade?'],
        confidence: 0.72,
      })
    ).toBe(true);
  });

  it('returns true when lifecycle_state is absent (legacy message defaults to committed)', () => {
    expect(
      shouldRenderFollowups({
        // no lifecycle_state
        followups: ['Legacy followup'],
        confidence: 0.6,
      })
    ).toBe(true);
  });
});

// ── Combined rule interaction ──────────────────────────────────────────────

describe('shouldRenderFollowups — multiple rules fail simultaneously', () => {
  it('returns false when rejected AND empty followups', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'rejected',
        followups: [],
        confidence: 0.1,
      })
    ).toBe(false);
  });

  it('returns false when all three blocking conditions apply', () => {
    expect(
      shouldRenderFollowups({
        lifecycle_state: 'rejected',
        followups: [],
        confidence: 0.1,
      })
    ).toBe(false);
  });
});
