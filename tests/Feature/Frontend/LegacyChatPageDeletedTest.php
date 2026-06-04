<?php

declare(strict_types=1);

namespace Tests\Feature\Frontend;

use Tests\TestCase;

/**
 * Pin the audit item G invariant: the legacy 1,585-line
 * `resources/js/Pages/Chat.tsx` was deleted on 2026-06-03 and must
 * NOT come back.
 *
 * Threat model
 * ------------
 * The replacement is `resources/js/Pages/Foundry/Chat.tsx` (the
 * Foundry-shell version). The legacy file at `Pages/Chat.tsx` was
 * dead — no `Inertia::render('Chat', ...)` callers existed in the
 * Laravel codebase, and the only references to it were:
 *   - the file itself,
 *   - its sibling test at `Pages/__tests__/Chat.test.tsx`,
 *   - two "Not yet ported from legacy Pages/Chat.tsx" docstrings
 *     in the new Foundry/Chat.tsx (also scrubbed).
 *
 * Why pin it
 * ----------
 * The risk: a future contributor doing "quick chat tweak" greps for
 * "Pages/Chat" and re-creates a Pages/Chat.tsx scaffold because they
 * expect the conventional Inertia layout. Without this guard the
 * dead-code tree grows back, and operators get two chat pages
 * shipped in the bundle.
 *
 * This is a file-content test: zero DB, fast, runs in CI on every PR.
 * The error message tells the contributor exactly where the LIVE chat
 * lives so they don't have to read the audit report to know.
 */
class LegacyChatPageDeletedTest extends TestCase
{
    public function test_legacy_pages_chat_tsx_is_deleted(): void
    {
        $legacy = base_path('resources/js/Pages/Chat.tsx');

        $this->assertFileDoesNotExist(
            $legacy,
            'resources/js/Pages/Chat.tsx must stay deleted (audit item G, '
            .'2026-06-03). The LIVE chat surface is '
            .'resources/js/Pages/Foundry/Chat.tsx — extend that. '
            .'Re-creating Pages/Chat.tsx ships a 1.5K-line dead duplicate '
            .'in the bundle and reintroduces the import confusion the '
            .'deletion was meant to close.',
        );
    }

    public function test_legacy_chat_test_is_deleted(): void
    {
        $legacy = base_path('resources/js/Pages/__tests__/Chat.test.tsx');

        $this->assertFileDoesNotExist(
            $legacy,
            'resources/js/Pages/__tests__/Chat.test.tsx must stay deleted. '
            .'Its sole purpose was exercising the deleted Pages/Chat.tsx; '
            .'re-creating it (or restoring it from git) without also '
            .'restoring Chat.tsx will fail the import. Tests for the LIVE '
            .'chat surface belong next to Foundry/Chat.tsx.',
        );
    }

    public function test_foundry_chat_tsx_is_the_canonical_surface(): void
    {
        // Belt-and-suspenders: if someone deletes the Foundry version
        // thinking it's the dead one, the chat experience silently
        // breaks. Pin the live file's existence too so the guard runs
        // in both directions.
        $live = base_path('resources/js/Pages/Foundry/Chat.tsx');

        $this->assertFileExists(
            $live,
            'resources/js/Pages/Foundry/Chat.tsx must exist — it is the '
            .'canonical chat page since the audit item G cleanup. If you '
            .'deleted it, restore it from git; the deletion was the WRONG '
            .'Chat.tsx.',
        );
    }

    public function test_no_dangling_legacy_chat_references_in_foundry(): void
    {
        // The scrub pass removed two comments in Foundry/Chat.tsx that
        // referenced "legacy Pages/Chat.tsx". If they come back, the
        // dead file's ghost lingers in docstrings and future readers
        // chase a phantom file.
        $live = base_path('resources/js/Pages/Foundry/Chat.tsx');
        $contents = (string) file_get_contents($live);

        $this->assertStringNotContainsString(
            'legacy Pages/Chat.tsx',
            $contents,
            'Foundry/Chat.tsx must not reference "legacy Pages/Chat.tsx" '
            .'— the legacy file no longer exists. Either rewrite the '
            .'reference (the original text is in `git log` if you need '
            .'it) or remove the comment.',
        );
    }
}
