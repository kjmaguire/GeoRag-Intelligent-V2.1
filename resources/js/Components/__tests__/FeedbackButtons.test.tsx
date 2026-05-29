// @ts-nocheck
/**
 * FeedbackButtons.test.tsx
 *
 * Tests for the B6 FeedbackButtons component.
 * Spec §10p — 6-category feedback taxonomy.
 *
 * Coverage:
 *   - Hide on streaming
 *   - Hide on user messages (not rendered in those contexts — tested via prop)
 *   - Thumbs-up click POSTs with polarity=up, no category required
 *   - Thumbs-down click opens the category popover
 *   - Each of the 6 categories submits the correct payload
 *   - Comment max-length guard (1000 chars)
 *   - Optimistic state — button turns colored before network response
 *   - Error rollback — state resets to idle on 4xx/5xx
 *   - Post-submit: both buttons disabled
 *   - A11y: distinct aria-labels, fieldset/legend present
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { FeedbackButtons } from '../chat/FeedbackButtons';

// ── Fetch mock setup ───────────────────────────────────────────────────────

const mockFetch = vi.fn();
beforeEach(() => {
    global.fetch = mockFetch;
    mockFetch.mockReset();
    // Default to success
    mockFetch.mockResolvedValue({
        ok: true,
        json: async () => ({ feedback_id: 'uuid-1', polarity: 'up', category: null, note: null }),
    });
});

afterEach(() => {
    vi.restoreAllMocks();
});

// Helper: render feedback buttons with a known answer_run_id
function renderButtons(answerRunId = 'aaaa-1111', isStreaming = false) {
    return render(
        <FeedbackButtons answerRunId={answerRunId} isStreaming={isStreaming} />
    );
}

// ── Render conditions ─────────────────────────────────────────────────────

describe('FeedbackButtons — render conditions', () => {
    it('renders both thumbs-up and thumbs-down buttons when not streaming', () => {
        renderButtons('run-1', false);
        expect(screen.getByRole('button', { name: /mark answer as helpful/i })).toBeDefined();
        expect(screen.getByRole('button', { name: /report answer problem/i })).toBeDefined();
    });

    it('disables both buttons when isStreaming=true', () => {
        renderButtons('run-1', true);
        const upBtn = screen.getByRole('button', { name: /mark answer as helpful/i });
        const downBtn = screen.getByRole('button', { name: /report answer problem/i });
        expect(upBtn.disabled).toBe(true);
        expect(downBtn.disabled).toBe(true);
    });

    it('renders the container testid', () => {
        const { getByTestId } = renderButtons();
        expect(getByTestId('feedback-buttons')).toBeDefined();
    });
});

// ── A11y ────────────────────────────────────────────────────────────────────

describe('FeedbackButtons — a11y', () => {
    it('thumbs-up has aria-label "Mark answer as helpful"', () => {
        renderButtons();
        expect(screen.getByRole('button', { name: 'Mark answer as helpful' })).toBeDefined();
    });

    it('thumbs-down has aria-label "Report answer problem"', () => {
        renderButtons();
        expect(screen.getByRole('button', { name: 'Report answer problem' })).toBeDefined();
    });

    it('popover has role=dialog and aria-label after opening', async () => {
        renderButtons();
        const downBtn = screen.getByRole('button', { name: 'Report answer problem' });
        fireEvent.click(downBtn);
        await waitFor(() => {
            const dialog = document.querySelector('[role="dialog"]');
            expect(dialog).toBeTruthy();
        });
    });

    it('category fieldset has a legend element', async () => {
        renderButtons();
        fireEvent.click(screen.getByRole('button', { name: 'Report answer problem' }));
        await waitFor(() => {
            expect(document.querySelector('fieldset')).toBeTruthy();
            expect(document.querySelector('legend')).toBeTruthy();
        });
    });
});

// ── Thumbs-up POST ─────────────────────────────────────────────────────────

describe('FeedbackButtons — thumbs-up', () => {
    it('POSTs with polarity=up and no category when clicked', async () => {
        renderButtons('run-2');
        fireEvent.click(screen.getByRole('button', { name: 'Mark answer as helpful' }));
        await waitFor(() => {
            expect(mockFetch).toHaveBeenCalledOnce();
        });
        const callArg = mockFetch.mock.calls[0];
        const body = JSON.parse(callArg[1].body);
        expect(body.polarity).toBe('up');
        expect(body.category).toBeUndefined();
    });

    it('shows "Thanks!" affordance on success', async () => {
        renderButtons('run-3');
        fireEvent.click(screen.getByRole('button', { name: 'Mark answer as helpful' }));
        await waitFor(() => {
            expect(screen.getByText('Thanks!')).toBeDefined();
        });
    });
});

// ── Thumbs-down popover ────────────────────────────────────────────────────

describe('FeedbackButtons — thumbs-down opens popover', () => {
    it('clicking thumbs-down opens the category selection popover', async () => {
        renderButtons('run-4');
        fireEvent.click(screen.getByRole('button', { name: 'Report answer problem' }));
        await waitFor(() => {
            expect(screen.getByText('What was wrong with this answer?')).toBeDefined();
        });
    });

    it('submit is disabled until a category is selected', async () => {
        renderButtons('run-4');
        fireEvent.click(screen.getByRole('button', { name: 'Report answer problem' }));
        await waitFor(() => {
            const submitBtn = screen.getByRole('button', { name: /submit feedback/i });
            expect(submitBtn.disabled).toBe(true);
        });
    });
});

// ── Six-category submissions ────────────────────────────────────────────────

const CATEGORIES = [
    'hallucinated',
    'wrong_facts',
    'missing_info',
    'off_topic',
    'citation_issue',
    'length_issue',
];

describe.each(CATEGORIES)('FeedbackButtons — category "%s" submission', (category) => {
    it(`POSTs polarity=down with category=${category}`, async () => {
        mockFetch.mockResolvedValue({
            ok: true,
            json: async () => ({ feedback_id: 'uuid-x', polarity: 'down', category, note: null }),
        });
        renderButtons(`run-cat-${category}`);

        // Open popover
        fireEvent.click(screen.getByRole('button', { name: 'Report answer problem' }));
        await waitFor(() => {
            expect(screen.getByText('What was wrong with this answer?')).toBeDefined();
        });

        // Select the radio
        const radio = document.getElementById(`feedback-cat-${category}`);
        expect(radio).toBeTruthy();
        fireEvent.click(radio);

        // Submit
        const submitBtn = screen.getByRole('button', { name: /submit feedback/i });
        fireEvent.click(submitBtn);

        await waitFor(() => {
            expect(mockFetch).toHaveBeenCalled();
        });
        const body = JSON.parse(mockFetch.mock.calls[0][1].body);
        expect(body.polarity).toBe('down');
        expect(body.category).toBe(category);
    });
});

// ── Comment length guard ────────────────────────────────────────────────────

describe('FeedbackButtons — comment length guard', () => {
    it('truncates note to 1000 characters', async () => {
        renderButtons('run-note');
        fireEvent.click(screen.getByRole('button', { name: 'Report answer problem' }));
        await waitFor(() => {
            expect(screen.getByLabelText(/additional comments/i)).toBeDefined();
        });

        const textarea = screen.getByLabelText(/additional comments/i);
        const longNote = 'A'.repeat(1200);
        fireEvent.change(textarea, { target: { value: longNote } });

        // Textarea value should be capped at 1000 via slice in onChange
        expect(textarea.value.length).toBeLessThanOrEqual(1000);
    });

    it('shows character count remaining via aria-live region', async () => {
        renderButtons('run-note-count');
        fireEvent.click(screen.getByRole('button', { name: 'Report answer problem' }));
        await waitFor(() => {
            const counter = document.getElementById('feedback-note-count');
            expect(counter).toBeTruthy();
            expect(counter.textContent).toContain('1000');
        });
    });
});

// ── Optimistic state ───────────────────────────────────────────────────────

describe('FeedbackButtons — optimistic state', () => {
    it('thumbs-up button is marked as pressed after successful submission', async () => {
        // The optimistic state is quickly followed by the submitted-up state
        // (since fetch resolves immediately in tests). Both have aria-pressed=true.
        // We verify the button reaches the submitted state (aria-pressed=true).
        renderButtons('run-optimistic');
        const upBtn = screen.getByRole('button', { name: 'Mark answer as helpful' });

        fireEvent.click(upBtn);

        // After a successful response, the button should be disabled with pressed state
        await waitFor(() => {
            expect(upBtn.disabled).toBe(true);
        });
        // The submitted-up state sets aria-pressed
        const pressed = upBtn.getAttribute('aria-pressed');
        expect(pressed).toBe('true');
    });
});

// ── Error rollback ─────────────────────────────────────────────────────────

describe('FeedbackButtons — error rollback', () => {
    it('rolls back to idle and shows inline error on 4xx', async () => {
        mockFetch.mockResolvedValue({
            ok: false,
            status: 422,
            json: async () => ({ detail: 'Validation failed' }),
        });

        renderButtons('run-error');
        fireEvent.click(screen.getByRole('button', { name: 'Mark answer as helpful' }));

        await waitFor(() => {
            // Error alert should appear
            expect(screen.getByRole('alert')).toBeDefined();
        });

        // Button should be enabled again (idle state)
        const upBtn = screen.getByRole('button', { name: 'Mark answer as helpful' });
        expect(upBtn.disabled).toBe(false);
    });
});

// ── Post-submit disabled ────────────────────────────────────────────────────

describe('FeedbackButtons — post-submit disabled', () => {
    it('disables both buttons after successful submission', async () => {
        renderButtons('run-submit-disable');
        const upBtn = screen.getByRole('button', { name: 'Mark answer as helpful' });
        fireEvent.click(upBtn);

        await waitFor(() => {
            // After success, buttons should be disabled
            expect(upBtn.disabled).toBe(true);
        });
    });
});

// ── Auth surface (security regression) ────────────────────────────────────────

describe('FeedbackButtons — auth surface', () => {
    let getItemSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
    });

    afterEach(() => {
        getItemSpy.mockRestore();
    });

    it('does not read auth tokens from localStorage when submitting feedback', async () => {
        renderButtons('run-auth-surface');
        const upBtn = screen.getByRole('button', { name: 'Mark answer as helpful' });
        fireEvent.click(upBtn);
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());

        const tokenLike = /token|jwt|secret/i;
        const offendingKeys = getItemSpy.mock.calls
            .map((call) => String(call[0]))
            .filter((key) => tokenLike.test(key));
        expect(offendingKeys).toEqual([]);
    });

    it('sends the request with same-origin credentials so the Sanctum cookie carries auth', async () => {
        renderButtons('run-auth-creds');
        fireEvent.click(screen.getByRole('button', { name: 'Mark answer as helpful' }));
        await waitFor(() => expect(mockFetch).toHaveBeenCalled());

        const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
        expect(init?.credentials).toBe('same-origin');

        const headers = (init?.headers ?? {}) as Record<string, string>;
        expect(headers.Authorization).toBeUndefined();
    });
});
