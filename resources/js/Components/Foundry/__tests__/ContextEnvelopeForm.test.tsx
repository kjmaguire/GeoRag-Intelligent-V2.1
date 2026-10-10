/**
 * ContextEnvelopeForm — the "Specific objects" box.
 *
 * It was a controlled input fed `list.join(', ')`, with the list parsed from
 * every keystroke. The "," typed after "DDH-07" parsed to the same one-item list,
 * the box re-rendered as "DDH-07", and the separator was gone: a second hole id
 * could not be typed, only pasted, and a space inside an id vanished the same way.
 */
import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
    ContextEnvelopeForm,
    EMPTY_ENVELOPE,
    parseSpecificObjects,
    type ContextEnvelope,
} from '../ContextEnvelopeForm';

afterEach(cleanup);

const project = { project_id: 'p-1', project_name: 'Red Star', slug: 'red-star' };

/** Holds the envelope the way Chat does, and reports every value the form emits. */
function Harness({ emitted }: { emitted: ContextEnvelope[] }) {
    const [envelope, setEnvelope] = useState<ContextEnvelope>(EMPTY_ENVELOPE);
    return (
        <>
            <ContextEnvelopeForm
                project={project}
                value={envelope}
                onChange={(next) => {
                    emitted.push(next);
                    setEnvelope(next);
                }}
            />
            <button type="button" onClick={() => setEnvelope({ ...EMPTY_ENVELOPE, specific_objects: ['X-1', 'X-2'] })}>
                load saved
            </button>
            <button type="button" onClick={() => setEnvelope(EMPTY_ENVELOPE)}>
                clear
            </button>
        </>
    );
}

function openForm(emitted: ContextEnvelope[] = []) {
    render(<Harness emitted={emitted} />);
    fireEvent.click(screen.getByRole('button', { name: /Context/ }));
    return screen.getByLabelText(/Specific objects/) as HTMLInputElement;
}

/** Type `text` one character at a time, asserting the box shows exactly what was typed. */
function typeSlowly(input: HTMLInputElement, text: string) {
    for (let i = 1; i <= text.length; i++) {
        const typed = text.slice(0, i);
        fireEvent.change(input, { target: { value: typed } });
        expect(input.value).toBe(typed);
    }
}

describe('Specific objects box', () => {
    it('lets a second hole id be typed: the comma and the space after it stay', () => {
        const emitted: ContextEnvelope[] = [];
        const input = openForm(emitted);

        typeSlowly(input, 'DDH-07, DDH-08, DDH-12');

        expect(emitted.at(-1)?.specific_objects).toEqual(['DDH-07', 'DDH-08', 'DDH-12']);
    });

    it('keeps a space inside an id', () => {
        const emitted: ContextEnvelope[] = [];
        const input = openForm(emitted);

        typeSlowly(input, 'PLS 22-08');

        expect(emitted.at(-1)?.specific_objects).toEqual(['PLS 22-08']);
    });

    it('emits a trimmed list without empties while the box shows exactly what was typed', () => {
        const emitted: ContextEnvelope[] = [];
        const input = openForm(emitted);

        fireEvent.change(input, { target: { value: ' a ,, b , c ,' } });

        expect(input.value).toBe(' a ,, b , c ,');
        expect(emitted.at(-1)?.specific_objects).toEqual(['a', 'b', 'c']);
    });

    it('shows a list that is set from outside, and clears when it is cleared', () => {
        const input = openForm();
        typeSlowly(input, 'typed-by-hand');

        fireEvent.click(screen.getByRole('button', { name: 'load saved' }));
        expect(input.value).toBe('X-1, X-2');

        fireEvent.click(screen.getByRole('button', { name: 'clear' }));
        expect(input.value).toBe('');
    });

    it('does not rewrite the box when the parent hands back the list it just received', () => {
        const input = openForm();

        fireEvent.change(input, { target: { value: 'A-1,' } });
        // The parent re-rendered with ['A-1']; the trailing comma must survive.
        expect(input.value).toBe('A-1,');
        fireEvent.change(input, { target: { value: 'A-1, ' } });
        expect(input.value).toBe('A-1, ');
    });

    it('counts the field as populated only once it holds an object', () => {
        const input = openForm();
        expect(screen.getByRole('button', { name: /Context/ })).toHaveTextContent('0/12');

        fireEvent.change(input, { target: { value: ' , ' } });
        expect(screen.getByRole('button', { name: /Context/ })).toHaveTextContent('0/12');

        fireEvent.change(input, { target: { value: 'DDH-07' } });
        expect(screen.getByRole('button', { name: /Context/ })).toHaveTextContent('1/12');
    });
});

describe('parseSpecificObjects', () => {
    it('splits on commas and newlines, trims, drops empties, keeps inner spaces', () => {
        expect(parseSpecificObjects('DDH-07, DDH-08\nPLS 22-08 ,, ')).toEqual(['DDH-07', 'DDH-08', 'PLS 22-08']);
        expect(parseSpecificObjects('')).toEqual([]);
        expect(parseSpecificObjects(' , \n ')).toEqual([]);
    });
});
