import { describe, it, expect } from 'vitest';
import {
    SSE_VOCABULARY,
    createDeltaBuffer,
    isHeartbeat,
    isUncitedAnswer,
    normaliseValidationState,
    readPromptParam,
    toPersistedMessage,
} from '../chatStream';

describe('SSE vocabulary', () => {
    it('is exactly the six names the producer and the relay use', () => {
        expect([...SSE_VOCABULARY]).toEqual(['status', 'bind', 'delta', 'citation', 'completed', 'failed']);
    });
});

describe('createDeltaBuffer (CHAT-15)', () => {
    it('renders tokens in token_seq order, not arrival order', () => {
        const buf = createDeltaBuffer();
        buf.add('grade ', 1, 'e2');
        buf.add('The ', 0, 'e1');
        expect(buf.add('is 0.21%', 2, 'e3')).toBe('The grade is 0.21%');
    });

    it('drops a re-delivered frame', () => {
        const buf = createDeltaBuffer();
        buf.add('The ', 0, 'e1');
        expect(buf.add('The ', 0, 'e1')).toBeNull();
        expect(buf.text()).toBe('The ');
    });

    it('does not stall on a gap (a skipped sentinel seq)', () => {
        const buf = createDeltaBuffer();
        buf.add('A', 0, 'e1');
        expect(buf.add('C', 2, 'e3')).toBe('AC');
    });

    it('stays correct over a long in-order stream and a late out-of-order token', () => {
        const buf = createDeltaBuffer();
        let expected = '';
        for (let i = 0; i < 5000; i++) {
            if (i === 2500) continue; // held back, arrives late below
            const tok = `t${i} `;
            expected += tok;
            expect(buf.add(tok, i, `e${i}`)).not.toBeNull();
        }
        expect(buf.text()).toBe(expected);
        buf.add('t2500 ', 2500, 'e2500');
        const full = Array.from({ length: 5000 }, (_, i) => `t${i} `).join('');
        expect(buf.text()).toBe(full);
    });

    it('puts un-sequenced tokens after sequenced ones whatever the arrival order', () => {
        const buf = createDeltaBuffer();
        buf.add('x', undefined, null);
        buf.add('B', 1, 'e2');
        buf.add('A', 0, 'e1');
        expect(buf.text()).toBe('ABx');
    });

    it('keeps arrival order for tokens without a seq', () => {
        const buf = createDeltaBuffer();
        buf.add('x', undefined, null);
        buf.add('y', undefined, null);
        expect(buf.text()).toBe('xy');
    });
});

describe('isUncitedAnswer (CHAT-16)', () => {
    it('flags a non-refused answer with no citations', () => {
        expect(isUncitedAnswer([], null)).toBe(true);
        expect(isUncitedAnswer(undefined, null)).toBe(true);
    });

    it('does not flag a cited answer or a refusal', () => {
        expect(isUncitedAnswer([{ citation_id: '[DATA-1]' }], null)).toBe(false);
        expect(isUncitedAnswer([], { type: 'refusal' })).toBe(false);
    });
});

describe('normaliseValidationState', () => {
    it('treats anything but an explicit verdict as unverified', () => {
        expect(normaliseValidationState('clean')).toBe('clean');
        expect(normaliseValidationState('flagged')).toBe('flagged');
        expect(normaliseValidationState(undefined)).toBe('unverified');
        expect(normaliseValidationState('CLEAN')).toBe('unverified');
    });
});

describe('isHeartbeat (CHAT-6)', () => {
    it('recognises only a status frame marked heartbeat', () => {
        expect(isHeartbeat({ event: 'status', heartbeat: true, message: 'x' })).toBe(true);
        expect(isHeartbeat({ event: 'status', message: 'x' })).toBe(false);
        expect(isHeartbeat({ event: 'delta', heartbeat: true })).toBe(false);
    });
});

describe('readPromptParam (FE-4)', () => {
    it('reads the copilot hand-off', () => {
        expect(readPromptParam('?prompt=Summarise%20the%20ore%20zones')).toBe('Summarise the ore zones');
        expect(readPromptParam('?thread=abc')).toBe('');
        expect(readPromptParam('')).toBe('');
    });
});

describe('toPersistedMessage (CHAT-4 / CHAT-9)', () => {
    it('keeps every verdict the page renders and an empty failed turn', () => {
        const out = toPersistedMessage({
            role: 'assistant',
            content: '',
            citations: [],
            confidence: null,
            validationState: 'flagged',
            answer_run_id: null,
            error: 'The query timed out.',
            errorCode: 'TIMEOUT',
            refusalPayload: null,
            citationsMissing: null,
        });
        expect(out.content).toBe('');
        expect(out.metadata).toMatchObject({
            validation_state: 'flagged',
            error: 'The query timed out.',
            error_code: 'TIMEOUT',
        });
    });
});
