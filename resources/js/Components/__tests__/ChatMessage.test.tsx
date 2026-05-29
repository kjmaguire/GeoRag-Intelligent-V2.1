/**
 * Tests for ChatMessage and its internal helpers.
 *
 * parseSegments and citationStyle are not exported from the module, so we
 * exercise them through component rendering rather than calling them
 * directly.  The one exception is the regex coverage test, which we verify
 * via rendered output.
 *
 * InlineViz is mocked so that MapLibre / Plotly don't need to boot in jsdom.
 * react-markdown is kept real (it's pure JS) but we assert on text nodes
 * and button elements — no markdown-specific assertions needed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

// Mock heavy dependencies that can't run in jsdom
vi.mock('./InlineViz', () => ({
    default: () => <div data-testid="inline-viz-mock" />,
}));
vi.mock('@/Components/InlineViz', () => ({
    default: () => <div data-testid="inline-viz-mock" />,
}));

// Import after mocks are declared
import ChatMessage from '../ChatMessage';

// ── Helper to build minimal message objects ───────────────────────────────

function makeMsg(overrides: Record<string, unknown> = {}) {
    return {
        id: 1,
        role: 'assistant' as const,
        content: 'Hello world',
        ...overrides,
    };
}

// ── parseSegments — exercised via rendered CitationChip buttons ───────────

describe('parseSegments (via render)', () => {
    it('renders alternating text and citation chips from mixed content', () => {
        const msg = makeMsg({ content: 'foo [NI43-1] bar [PGEO-2] baz' });
        render(<ChatMessage message={msg} />);

        // Two chips should appear
        const chips = screen.getAllByRole('button', { name: /View source/ });
        expect(chips).toHaveLength(2);
        // Chunk 2+: chip text is the index as a superscript; full id is on aria-label
        expect(chips[0]).toHaveTextContent('1');
        expect(chips[0]).toHaveAttribute('aria-label', 'View source [NI43:1]');
        expect(chips[1]).toHaveTextContent('2');
        expect(chips[1]).toHaveAttribute('aria-label', 'View source [PGEO:2]');

        // Plain text segments should be present somewhere in the container
        expect(screen.getByText(/foo/)).toBeTruthy();
        expect(screen.getByText(/baz/)).toBeTruthy();
    });

    it('renders no chips when message contains no citations', () => {
        const msg = makeMsg({ content: 'No citations here.' });
        render(<ChatMessage message={msg} />);
        expect(screen.queryAllByRole('button', { name: /View source/ })).toHaveLength(0);
    });

    it('renders a chip for a citation that fills the whole content string', () => {
        const msg = makeMsg({ content: '[DATA-99]' });
        render(<ChatMessage message={msg} />);
        const chips = screen.getAllByRole('button', { name: /View source/ });
        expect(chips).toHaveLength(1);
        // Chunk 2+: chip displays the index as superscript, full id surfaces via aria-label
        expect(chips[0]).toHaveTextContent('99');
        expect(chips[0]).toHaveAttribute('aria-label', 'View source [DATA:99]');
    });
});

// ── Citation regex — all four types ──────────────────────────────────────

describe('citation regex — all four types', () => {
    // [kind, input-form (dash), displayed-index, colon-form (Chunk 2+ aria-label)]
    const cases: [string, string, string, string][] = [
        ['NI43',  '[NI43-7]',  '7',  '[NI43:7]'],
        ['PUB',   '[PUB-42]',  '42', '[PUB:42]'],
        ['DATA',  '[DATA-X1]', 'X1', '[DATA:X1]'],
        ['PGEO',  '[PGEO-3]',  '3',  '[PGEO:3]'],
    ];

    for (const [type, raw, idx, colon] of cases) {
        it(`matches ${type} citation format`, () => {
            const msg = makeMsg({ content: `Before ${raw} after` });
            render(<ChatMessage message={msg} />);
            const chip = screen.getByRole('button', { name: `View source ${colon}` });
            expect(chip).toHaveTextContent(idx);
        });
    }

    it('does not create a chip for an unknown prefix like [FOO-1]', () => {
        const msg = makeMsg({ content: 'Test [FOO-1] text' });
        render(<ChatMessage message={msg} />);
        expect(screen.queryAllByRole('button', { name: /View source/ })).toHaveLength(0);
    });
});

// ── PGEO citation chip — rose color palette ───────────────────────────────

describe('PGEO citation chip styling', () => {
    it('button has bg-rose-900/60 class', () => {
        const msg = makeMsg({ content: '[PGEO-1]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [PGEO:1]' });
        expect(chip.className).toContain('bg-rose-900/60');
    });

    it('button has text-rose-300 class', () => {
        const msg = makeMsg({ content: '[PGEO-1]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [PGEO:1]' });
        expect(chip.className).toContain('text-rose-300');
    });

    it('button has border-rose-600/50 class', () => {
        const msg = makeMsg({ content: '[PGEO-1]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [PGEO:1]' });
        expect(chip.className).toContain('border-rose-600/50');
    });
});

// ── All four citation palettes ────────────────────────────────────────────

describe('citation color palettes per type', () => {
    it('NI43 chip has amber palette', () => {
        const msg = makeMsg({ content: '[NI43-5]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [NI43:5]' });
        expect(chip.className).toContain('bg-amber-900/60');
        expect(chip.className).toContain('text-amber-300');
        expect(chip.className).toContain('border-amber-600/50');
    });

    it('PUB chip has blue palette', () => {
        const msg = makeMsg({ content: '[PUB-10]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [PUB:10]' });
        expect(chip.className).toContain('bg-blue-900/60');
        expect(chip.className).toContain('text-blue-300');
        expect(chip.className).toContain('border-blue-600/50');
    });

    it('DATA chip has green palette', () => {
        const msg = makeMsg({ content: '[DATA-3]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [DATA:3]' });
        expect(chip.className).toContain('bg-green-900/60');
        expect(chip.className).toContain('text-green-300');
        expect(chip.className).toContain('border-green-600/50');
    });

    it('PGEO chip has rose palette', () => {
        const msg = makeMsg({ content: '[PGEO-7]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [PGEO:7]' });
        expect(chip.className).toContain('bg-rose-900/60');
        expect(chip.className).toContain('text-rose-300');
        expect(chip.className).toContain('border-rose-600/50');
    });
});

// ── onCitationClick handler ───────────────────────────────────────────────

describe('onCitationClick handler', () => {
    it('calls onCitationClick with the raw citation string when a chip is clicked', () => {
        const handler = vi.fn();
        const msg = makeMsg({ content: 'See [NI43-42] for details' });
        render(<ChatMessage message={msg} onCitationClick={handler} />);

        const chip = screen.getByRole('button', { name: 'View source [NI43:42]' });
        fireEvent.click(chip);

        expect(handler).toHaveBeenCalledTimes(1);
        expect(handler).toHaveBeenCalledWith('[NI43-42]');
    });

    it('calls handler with each citation when multiple chips are clicked', () => {
        const handler = vi.fn();
        const msg = makeMsg({ content: '[PUB-1] and [PGEO-2]' });
        render(<ChatMessage message={msg} onCitationClick={handler} />);

        fireEvent.click(screen.getByRole('button', { name: 'View source [PUB:1]' }));
        fireEvent.click(screen.getByRole('button', { name: 'View source [PGEO:2]' }));

        expect(handler).toHaveBeenNthCalledWith(1, '[PUB-1]');
        expect(handler).toHaveBeenNthCalledWith(2, '[PGEO-2]');
    });

    it('does not throw when onCitationClick is not provided', () => {
        const msg = makeMsg({ content: '[DATA-1]' });
        render(<ChatMessage message={msg} />);
        const chip = screen.getByRole('button', { name: 'View source [DATA:1]' });
        expect(() => fireEvent.click(chip)).not.toThrow();
    });
});

// ── ConfidenceIndicator ───────────────────────────────────────────────────

describe('ConfidenceIndicator', () => {
    it('score 0.85 renders the green bar and "High" in the tooltip', () => {
        const msg = makeMsg({ confidence: 0.85 });
        render(<ChatMessage message={msg} />);

        // Find the indicator wrapper via its aria-label attribute
        const container = document.querySelector('[aria-label*="85 percent"]');
        expect(container).toBeTruthy();

        const bar = container!.querySelector('.bg-green-500');
        expect(bar).toBeTruthy();

        // Tooltip text is in a sibling span
        const tooltip = container!.querySelector('span.hidden');
        expect(tooltip?.textContent).toContain('High');
    });

    it('score 0.6 renders the amber bar', () => {
        const msg = makeMsg({ confidence: 0.6 });
        render(<ChatMessage message={msg} />);

        const container = document.querySelector('[aria-label*="60 percent"]');
        expect(container).toBeTruthy();
        const bar = container!.querySelector('.bg-amber-500');
        expect(bar).toBeTruthy();

        const tooltip = container!.querySelector('span.hidden');
        expect(tooltip?.textContent).toContain('Medium');
    });

    it('score 0.3 renders the red bar', () => {
        const msg = makeMsg({ confidence: 0.3 });
        render(<ChatMessage message={msg} />);

        const container = document.querySelector('[aria-label*="30 percent"]');
        expect(container).toBeTruthy();
        const bar = container!.querySelector('.bg-red-500');
        expect(bar).toBeTruthy();

        const tooltip = container!.querySelector('span.hidden');
        expect(tooltip?.textContent).toContain('Low');
    });

    it('renders nothing when confidence is null', () => {
        const msg = makeMsg({ confidence: null });
        const { container } = render(<ChatMessage message={msg} />);
        // No confidence bar element should be present
        expect(container.querySelector('[aria-label*="percent"]')).toBeNull();
    });

    it('renders nothing when confidence is undefined (not provided)', () => {
        const msg = makeMsg();
        const { container } = render(<ChatMessage message={msg} />);
        expect(container.querySelector('[aria-label*="percent"]')).toBeNull();
    });
});

// ── User vs assistant bubble structure ────────────────────────────────────

describe('ChatMessage layout', () => {
    it('user message has data-role="user"', () => {
        const msg = makeMsg({ role: 'user', content: 'Hello' });
        render(<ChatMessage message={msg} />);
        const wrapper = document.querySelector('[data-role="user"]');
        expect(wrapper).toBeTruthy();
    });

    it('assistant message has data-role="assistant"', () => {
        const msg = makeMsg({ role: 'assistant', content: 'Hi' });
        render(<ChatMessage message={msg} />);
        const wrapper = document.querySelector('[data-role="assistant"]');
        expect(wrapper).toBeTruthy();
    });

    it('renders "You" label for user messages', () => {
        const msg = makeMsg({ role: 'user', content: 'Hi' });
        render(<ChatMessage message={msg} />);
        expect(screen.getByText('You')).toBeTruthy();
    });

    it('renders "GeoRAG" label for assistant messages', () => {
        const msg = makeMsg({ role: 'assistant', content: 'Hi' });
        render(<ChatMessage message={msg} />);
        expect(screen.getByText('GeoRAG')).toBeTruthy();
    });
});
