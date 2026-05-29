<?php

declare(strict_types=1);

/*
|--------------------------------------------------------------------------
| Inertia Config Override — doc-phase 172
|--------------------------------------------------------------------------
|
| The Inertia package default points `pages.paths` at
| `resource_path('js/pages')` (lowercase). GeoRAG places its Inertia
| pages under `resources/js/Pages` (capital P) per the project's
| TypeScript-first React convention.
|
| Without this override, the testing-mode `ensure_pages_exist` check
| fails every admin-dashboard render assertion with "Inertia page
| component file [Admin/EvalDashboard] does not exist." even though
| the file is on disk — the finder just isn't looking at the right
| directory.
|
| Setting `ensure_pages_exist => false` at the top level is safe at
| runtime (the page-finder only runs when assertInertia is active) and
| the `paths` override pins the testing variant to the correct casing.
*/

return [
    'pages' => [
        // Runtime: inertia renderer reads from Vite — page-paths are
        // informational. Disabling the on-disk lookup here keeps the
        // production hot-path one syscall lighter.
        'ensure_pages_exist' => false,

        'paths' => [
            resource_path('js/Pages'),
        ],

        'extensions' => [
            'js',
            'jsx',
            'ts',
            'tsx',
        ],
    ],

    // Testing mode — leave the on-disk check ON (catches missing /
    // misnamed components in PRs) but point at the correct path.
    'testing' => [
        'ensure_pages_exist' => true,
        'page_paths' => [
            resource_path('js/Pages'),
        ],
        'page_extensions' => [
            'js',
            'jsx',
            'ts',
            'tsx',
        ],
    ],
];
