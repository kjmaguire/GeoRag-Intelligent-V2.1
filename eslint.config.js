import { Module } from 'node:module';
import { defineConfig } from 'eslint/config';
import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import prettier from 'eslint-config-prettier';

// typescript-eslint parses through the TypeScript *compiler API*, which
// TypeScript 7 (the native port that `tsc` and package.json use) does not
// ship: `require('typescript')` there resolves to a version stub only. The
// official compatibility package `@typescript/typescript6` re-exports the
// TS 6 API, so point typescript-eslint's `require('typescript')` at it.
// This affects parsing only; `tsc --noEmit` still runs TS 7. Delete this
// block (and the `overrides` entry in package.json, and the
// `@typescript/typescript6` devDependency) once typescript-eslint supports
// TS 7 -- its peer range is still `>=4.8.4 <6.1.0`.
const resolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
    return resolveFilename.call(this, request === 'typescript' ? '@typescript/typescript6' : request, ...rest);
};
const { default: tseslint } = await import('typescript-eslint');

export default defineConfig(
    { ignores: ['public/build/**', 'vendor/**', 'node_modules/**', 'storage/**', 'tests/load_k6/**'] },
    js.configs.recommended,
    tseslint.configs.recommended,
    reactHooks.configs.flat.recommended,
    {
        rules: {
            // `const { only, ...rest } = props` to keep a prop off a DOM
            // element is the idiom for omitting keys; the rule should not
            // call the omitted key unused.
            '@typescript-eslint/no-unused-vars': ['error', { ignoreRestSiblings: true }],

            // react-hooks 7 folds the React Compiler's checks into
            // `recommended`. This codebase does not run the Compiler, and
            // these two rules flag patterns that are deliberate here, each
            // needing a per-site redesign rather than a mechanical fix, so
            // they are warnings (a visible burn-down) instead of errors:
            //  - set-state-in-effect: `setX({ kind: 'loading' })` at the top
            //    of a fetch effect, and prop-to-state resets.
            //  - refs: the documented "latest value" ref assigned during
            //    render (WorkspaceMap, RasterLayers, NewProject) so that
            //    handlers registered once on a MapLibre map read current
            //    props. Moving the writes into effects would change timing.
            'react-hooks/set-state-in-effect': 'warn',
            'react-hooks/refs': 'warn',
        },
    },
    prettier,
);
