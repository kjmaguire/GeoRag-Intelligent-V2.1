import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { CitationMarker, type MarkerKind } from '../chat/CitationMarker';
import { CITATION_RE } from '../ChatMessage';

function makeCitation(overrides = {}) {
  return { citation_id: '[DATA:1]', citation_type: 'DATA', source_chunk_id: 'chunk-abc', document_title: 'Test Document', relevance_score: 0.85, ...overrides } as any;
}
// Kind -> icon rendering
describe('CitationMarker kind-based icon variants', () => {
    it('DATA kind renders a button', () => {
        render(<CitationMarker kind="DATA" id="1" onClick={vi.fn()} />);
        expect(screen.getByRole('button')).toBeTruthy();
    });
    it('NI43 kind renders a button', () => {
        render(<CitationMarker kind="NI43" id="7" onClick={vi.fn()} />);
        expect(screen.getByRole('button')).toBeTruthy();
    });
    it('PUB kind renders a button', () => {
        render(<CitationMarker kind="PUB" id="3" onClick={vi.fn()} />);
        expect(screen.getByRole('button')).toBeTruthy();
    });
    it('PGEO kind renders a button', () => {
        render(<CitationMarker kind="PGEO" id="2" onClick={vi.fn()} />);
        expect(screen.getByRole('button')).toBeTruthy();
    });
    it('ev kind renders a button', () => {
        render(<CitationMarker kind="ev" id="abc123-uuid" onClick={vi.fn()} />);
        expect(screen.getByRole('button')).toBeTruthy();
    });
});

describe('CitationMarker color palettes per kind', () => {
    const cases = [
        ['DATA',  'bg-green-900/60',  'text-green-300'],
        ['NI43',  'bg-amber-900/60',  'text-amber-300'],
        ['PUB',   'bg-blue-900/60',   'text-blue-300'],
        ['PGEO',  'bg-rose-900/60',   'text-rose-300'],
        ['ev',    'bg-violet-900/60', 'text-violet-300'],
    ];
    for (const [kind, bgClass, textClass] of cases) {
        it(kind + ' has correct bg class', () => {
            const { container } = render(<CitationMarker kind={kind as MarkerKind} id="1" onClick={vi.fn()} />);
            const btn = container.querySelector('button');
            expect(btn?.className).toContain(bgClass);
        });
        it(kind + ' has correct text class', () => {
            const { container } = render(<CitationMarker kind={kind as MarkerKind} id="1" onClick={vi.fn()} />);
            const btn = container.querySelector('button');
            expect(btn?.className).toContain(textClass);
        });
    }
});

describe('CitationMarker evidence_type icon override', () => {
    it('document_passage: aria-label contains Cite passage', () => {
        const cit = makeCitation({ evidence_type: 'document_passage' });
        render(<CitationMarker kind="DATA" id="1" citation={cit} onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('Cite passage');
    });
    it('structured_record: aria-label contains Cite table row', () => {
        const cit = makeCitation({ evidence_type: 'structured_record' });
        render(<CitationMarker kind="NI43" id="1" citation={cit} onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('Cite table row');
    });
    it('graph_edge: aria-label contains Cite graph edge', () => {
        const cit = makeCitation({ evidence_type: 'graph_edge' });
        render(<CitationMarker kind="PUB" id="1" citation={cit} onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('Cite graph edge');
    });
    it('map_feature: aria-label contains Cite map feature', () => {
        const cit = makeCitation({ evidence_type: 'map_feature' });
        render(<CitationMarker kind="PGEO" id="1" citation={cit} onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('Cite map feature');
    });
    it('no evidence_type: falls back to View source', () => {
        render(<CitationMarker kind="DATA" id="5" onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('View source');
    });
});

describe('CitationMarker aria-label document title', () => {
    it('includes document_title when citation provided', () => {
        const cit = makeCitation({ document_title: 'Technical Report 2024' });
        render(<CitationMarker kind="NI43" id="3" citation={cit} onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('Technical Report 2024');
    });
    it('aria-label contains marker text when no citation', () => {
        render(<CitationMarker kind="DATA" id="3" onClick={vi.fn()} />);
        expect(screen.getByRole('button').getAttribute('aria-label')).toContain('[DATA:3]');
    });
});

describe('CitationMarker onClick', () => {
    it('calls onClick with citation, kind, id', () => {
        const handler = vi.fn();
        const cit = makeCitation();
        render(<CitationMarker kind="DATA" id="1" citation={cit} onClick={handler} />);
        fireEvent.click(screen.getByRole('button'));
        expect(handler).toHaveBeenCalledTimes(1);
        expect(handler).toHaveBeenCalledWith(cit, 'DATA', '1');
    });
    it('calls onClick with null when no citation', () => {
        const handler = vi.fn();
        render(<CitationMarker kind="PUB" id="42" onClick={handler} />);
        fireEvent.click(screen.getByRole('button'));
        expect(handler).toHaveBeenCalledWith(null, 'PUB', '42');
    });
});

describe('CITATION_RE regex coverage', () => {
    beforeEach(() => { CITATION_RE.lastIndex = 0; });
    const colonForms = [['[NI43:1]','NI43','1'],['[PUB:42]','PUB','42'],['[DATA:7]','DATA','7'],['[PGEO:3]','PGEO','3']];
    for (const [raw, kind, id] of colonForms) {
        it('matches colon-form ' + raw, () => {
            CITATION_RE.lastIndex = 0;
            const m = CITATION_RE.exec(raw);
            expect(m).not.toBeNull();
            expect(m![1]).toBe(kind);
            expect(m![2]).toBe(id);
        });
    }
    const dashForms = [['[NI43-1]','NI43','1'],['[PUB-42]','PUB','42'],['[DATA-7]','DATA','7'],['[PGEO-3]','PGEO','3']];
    for (const [raw, kind, id] of dashForms) {
        it('matches dash-form ' + raw, () => {
            CITATION_RE.lastIndex = 0;
            const m = CITATION_RE.exec(raw);
            expect(m).not.toBeNull();
            expect(m![1]).toBe(kind);
            expect(m![2]).toBe(id);
        });
    }
    it('matches ev:uuid evidence id', () => {
        CITATION_RE.lastIndex = 0;
        const m = CITATION_RE.exec('[ev:abc123-def456]');
        expect(m).not.toBeNull();
        expect(m![1]).toBe('ev');
        expect(m![2]).toBe('abc123-def456');
    });
    it('matches ev:short-id', () => {
        CITATION_RE.lastIndex = 0;
        const m = CITATION_RE.exec('[ev:1234]');
        expect(m).not.toBeNull();
        expect(m![1]).toBe('ev');
    });
    it('does not match unknown prefix', () => {
        CITATION_RE.lastIndex = 0;
        expect(CITATION_RE.exec('[FOO:1]')).toBeNull();
    });
    it('does not match plain text', () => {
        CITATION_RE.lastIndex = 0;
        expect(CITATION_RE.exec('no citation here')).toBeNull();
    });
    it('matches multiple citations', () => {
        CITATION_RE.lastIndex = 0;
        const text = 'See [NI43:1] and [DATA-7] and [ev:abc]';
        const matches: RegExpExecArray[] = [];
        let m;
        while ((m = CITATION_RE.exec(text)) !== null) { matches.push(m); }
        expect(matches).toHaveLength(3);
        expect(matches[0][1]).toBe('NI43');
        expect(matches[1][1]).toBe('DATA');
        expect(matches[2][1]).toBe('ev');
    });
});
