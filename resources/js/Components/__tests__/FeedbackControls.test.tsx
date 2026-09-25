/**
 * FeedbackControls.test.tsx — §10p 👍/👎 + taxonomy + note.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import FeedbackControls from '@/Components/FeedbackControls';

describe('FeedbackControls', () => {
    let originalFetch: typeof globalThis.fetch;
    let fetchMock: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        originalFetch = globalThis.fetch;
        fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 201, json: async () => ({}) });
        globalThis.fetch = fetchMock as unknown as typeof fetch;
    });

    afterEach(() => {
        globalThis.fetch = originalFetch;
        vi.clearAllMocks();
    });

    it('renders nothing when answerRunId is null', () => {
        const { container } = render(<FeedbackControls answerRunId={null} />);
        expect(container.firstChild).toBeNull();
    });

    it('posts polarity=up immediately on thumbs-up click', async () => {
        render(<FeedbackControls answerRunId="run-1" />);
        fireEvent.click(screen.getByLabelText('Good answer'));
        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
        expect(fetchMock).toHaveBeenCalledWith(
            '/api/v1/answer-runs/run-1/feedback',
            expect.objectContaining({
                method: 'POST',
                body: JSON.stringify({ polarity: 'up', category: null, note: null }),
            })
        );
    });

    it('expands a category + note form on thumbs-down click without submitting yet', () => {
        render(<FeedbackControls answerRunId="run-1" />);
        fireEvent.click(screen.getByLabelText('Bad answer'));
        expect(screen.getByLabelText('Feedback category')).toBeInTheDocument();
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it('requires a category before the down-vote form submits', () => {
        render(<FeedbackControls answerRunId="run-1" />);
        fireEvent.click(screen.getByLabelText('Bad answer'));
        expect(screen.getByText('Submit feedback')).toBeDisabled();
    });

    it('submits polarity=down with category + note', async () => {
        render(<FeedbackControls answerRunId="run-1" />);
        fireEvent.click(screen.getByLabelText('Bad answer'));
        fireEvent.change(screen.getByLabelText('Feedback category'), { target: { value: 'citation_issue' } });
        fireEvent.change(screen.getByLabelText('Feedback note'), { target: { value: 'wrong source' } });
        fireEvent.click(screen.getByText('Submit feedback'));
        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
        expect(fetchMock).toHaveBeenCalledWith(
            '/api/v1/answer-runs/run-1/feedback',
            expect.objectContaining({
                body: JSON.stringify({ polarity: 'down', category: 'citation_issue', note: 'wrong source' }),
            })
        );
    });

    it('opens pre-filled to citation_issue when presetCategory is set', () => {
        render(
            <FeedbackControls answerRunId="run-1" presetCategory={{ category: 'citation_issue' }} />
        );
        expect(screen.getByLabelText('Feedback category')).toHaveValue('citation_issue');
    });

    it('shows an error message when the request fails', async () => {
        fetchMock.mockResolvedValue({ ok: false, status: 500 });
        render(<FeedbackControls answerRunId="run-1" />);
        fireEvent.click(screen.getByLabelText('Good answer'));
        await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Feedback failed to send.'));
    });
});
