import { describe, it, expect } from 'vitest';
import {
    SSE_VOCABULARY,
    createDeltaBuffer,
    isHeartbeat,
    isEmptySourceId,
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

/**
 * What GeoRAGResponse.citations really holds when nothing was retrieved. The
 * field has min_length=1, so the producer cannot send `[]`: response_assembler
 * appends this placeholder, and an empty document search yields the
 * `georag_reports:empty` id. The old tests sent `citations: []`, a frame that
 * never reaches the browser, so the warning they pinned could not fire.
 */
const placeholder = (sourceChunkId: string) => ({
    citation_id: '[DATA-1]',
    citation_type: 'DATA',
    source_chunk_id: sourceChunkId,
    document_title: 'No source retrieved',
    section: null,
    page: null,
    relevance_score: 0,
    corpus: 'internal_archive',
});
const REPORT_CHUNK =
    'georag_reports:5d0c4e0e-6f8e-5a64-9c3f-1f6f7b2f0a11:section=7:chunk=3f2c9d1e-0000-4000-8000-000000000001';
const chunkCitation = (sourceChunkId: string) => ({
    ...placeholder(sourceChunkId),
    citation_id: '[NI43-1]',
    citation_type: 'NI43',
});

describe('isUncitedAnswer (CHAT-16)', () => {
    it('flags an answer whose only citation is the no-tool-call placeholder', () => {
        expect(isUncitedAnswer([placeholder('no-tool-call')], null)).toBe(true);
    });

    it('flags an answer whose only citation is the empty-retrieval sentinel', () => {
        expect(isUncitedAnswer([placeholder('georag_reports:empty')], null)).toBe(true);
        expect(isUncitedAnswer([placeholder('pg_public_geoscience:empty')], null)).toBe(true);
    });

    it('flags an answer whose citations all point at zero-row tool results', () => {
        expect(isUncitedAnswer([placeholder('silver.samples:element=U3O8:count=0')], null)).toBe(true);
        expect(isUncitedAnswer([placeholder('silver.collars:miss'), placeholder('silver.collars:count=0')], null)).toBe(
            true,
        );
    });

    it('still flags the shapes the producer cannot send but a defect could', () => {
        expect(isUncitedAnswer([], null)).toBe(true);
        expect(isUncitedAnswer(undefined, null)).toBe(true);
        expect(isUncitedAnswer(null, null)).toBe(true);
        expect(isUncitedAnswer([{ citation_id: '[DATA-1]' }], null)).toBe(true); // no source_chunk_id at all
        expect(isUncitedAnswer([placeholder('')], null)).toBe(true);
    });

    it('does not flag an answer with a citation that points at evidence', () => {
        expect(isUncitedAnswer([chunkCitation(REPORT_CHUNK)], null)).toBe(false);
        expect(isUncitedAnswer([placeholder('silver.collars:count=7:first=abc')], null)).toBe(false);
    });

    it('does not flag a mixed frame: one real citation beside the sentinels', () => {
        expect(isUncitedAnswer([placeholder('silver.collars:miss'), chunkCitation(REPORT_CHUNK)], null)).toBe(false);
    });

    it('does not flag a refusal, whatever placeholder it carries', () => {
        expect(isUncitedAnswer([placeholder('no-tool-call')], { type: 'refusal' })).toBe(false);
        expect(
            isUncitedAnswer([placeholder('citation-rejected')], {
                type: 'refusal',
                reason_code: 'unsupported_by_sources',
            }),
        ).toBe(false);
        expect(isUncitedAnswer([], { type: 'refusal' })).toBe(false);
    });
});

describe('isEmptySourceId', () => {
    it.each([
        'no-tool-call',
        'georag_reports:empty',
        'pg_public_geoscience:empty',
        'silver.collars:miss',
        'citation-rejected',
        'provenance-rejected',
        'silver.samples:element=U3O8:count=0',
        'silver.collars:count=0',
        'silver.project_summary:project=p:rows=0:first_row=none',
        'silver.drill_traces:project=p:holes=0:first_collar=none:hole_filter=all',
        'silver.projects:slug=x:company=y:curves=0:reports=0',
        'silver.lithology_logs:intervals=0',
        '',
    ])('treats %j as evidence-free', (id) => {
        expect(isEmptySourceId(id)).toBe(true);
    });

    it.each([
        REPORT_CHUNK,
        'silver.collars:count=7:first=abc',
        'silver.lithology_logs:hole=PLS-22-08:collar=abc:intervals=0',
        'silver.samples:element=U3O8:count=12',
        'pg_mineral_occurrence:src:feature=1:pg_id=abc',
    ])('treats %j as evidence', (id) => {
        expect(isEmptySourceId(id)).toBe(false);
    });

    it('treats a non-string as evidence-free', () => {
        expect(isEmptySourceId(undefined)).toBe(true);
        expect(isEmptySourceId(null)).toBe(true);
        expect(isEmptySourceId(42)).toBe(true);
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
