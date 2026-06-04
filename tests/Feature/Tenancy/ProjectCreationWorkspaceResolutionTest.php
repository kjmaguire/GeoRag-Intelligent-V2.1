<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * Pin the audit item J1 invariant: ProjectController::store and
 * OnboardingController both resolve workspace_id via
 * User->defaultWorkspaceId() (workspace_user pivot, audit item A),
 * NOT the legacy $user->workspace_id column.
 *
 * Why it matters
 * --------------
 * Pre-item-A there was no users.workspace_id column at all — the
 * pattern $user->workspace_id was returning null on every call and the
 * `?? 'a0000000-...001'` fallback fired UNCONDITIONALLY. Every new
 * project landed in the same seeded default tenant regardless of which
 * workspace the user actually belonged to. Item A introduced the
 * workspace_user pivot + User->defaultWorkspaceId(); item J1 wires
 * those into the project-creation paths so multi-tenant deployments
 * actually scope new projects to the creator's workspace.
 *
 * Pattern is file-content assertions, matching the other tenancy
 * regression tests in this dir — runs without a live DB.
 */
class ProjectCreationWorkspaceResolutionTest extends TestCase
{
    private function projectControllerSrc(): string
    {
        $path = base_path('app/Http/Controllers/Api/V1/ProjectController.php');
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    private function onboardingControllerSrc(): string
    {
        $path = base_path('app/Http/Controllers/OnboardingController.php');
        $this->assertFileExists($path);

        return (string) file_get_contents($path);
    }

    public function test_project_store_uses_default_workspace_id_helper(): void
    {
        $contents = $this->projectControllerSrc();

        $this->assertStringContainsString(
            '$user->defaultWorkspaceId()',
            $contents,
            'ProjectController::store must call $user->defaultWorkspaceId() '
            .'(audit item A helper). The pre-J1 form `$user->workspace_id` '
            .'returns null because no such column exists on the users '
            .'table — the fallback then fired unconditionally and every '
            .'new project landed in the seeded default tenant.',
        );
    }

    public function test_project_store_does_not_reference_legacy_workspace_id_column(): void
    {
        $contents = $this->projectControllerSrc();

        // The bare `$user->workspace_id` access pattern (no parens, no
        // method call) is the legacy form. Match it via regex so we
        // don't false-positive on $project->workspace_id which is fine.
        $this->assertDoesNotMatchRegularExpression(
            '/\$(?:request->user\(\)|user)->workspace_id\b/',
            $contents,
            'ProjectController must not access $user->workspace_id (or '
            .'$request->user()->workspace_id). The legacy column never '
            .'existed; use $user->defaultWorkspaceId() instead. If you '
            .'see this assertion fail, you re-introduced the bug J1 '
            .'closed.',
        );
    }

    public function test_onboarding_step1_uses_default_workspace_id_helper(): void
    {
        $contents = $this->onboardingControllerSrc();

        // Step1 should resolve via defaultWorkspaceId() before falling
        // back to the seeded default. Pin the pattern.
        $this->assertStringContainsString(
            '$user->defaultWorkspaceId()',
            $contents,
            'OnboardingController must call $user->defaultWorkspaceId() '
            .'so step1 / step2 / AOI insert can scope to the user\'s real '
            .'workspace instead of the seeded default.',
        );
    }

    public function test_onboarding_default_uuid_appears_only_in_fallback_clauses(): void
    {
        $contents = $this->onboardingControllerSrc();

        // The literal MAY appear (as a true bootstrap fallback for fresh
        // single-tenant deploys), but ONLY in `?? '...'` contexts —
        // never as the unconditional RHS of an insert/assignment. The
        // easy way to catch the pre-J1 form is "literal directly inside
        // an insert array".
        $this->assertDoesNotMatchRegularExpression(
            "/'workspace_id'\\s*=>\\s*'a0000000-0000-0000-0000-000000000001'/",
            $contents,
            'OnboardingController must not hardcode the default workspace '
            .'UUID directly as an insert value. Use a variable resolved '
            .'via $user->defaultWorkspaceId() ?? <fallback> so a real '
            .'workspace member gets their own workspace_id on their '
            .'first project, not the seeded shared one.',
        );
    }

    public function test_fallback_remains_for_first_boot_bootstrap(): void
    {
        // Belt-and-suspenders: the `?? 'a0000000-...001'` last-ditch
        // fallback MUST stay so a truly fresh deployment (no pivot rows
        // yet) can still complete onboarding. Removing it would crash
        // step1 with a NOT NULL violation on the first ever signup.
        $contents = $this->onboardingControllerSrc();

        // Pint may reflow whitespace around the ?? operator; match the
        // load-bearing tokens with arbitrary whitespace/newline between.
        $this->assertMatchesRegularExpression(
            "/defaultWorkspaceId\\(\\)\\s*\\?\\?\\s*'a0000000-0000-0000-0000-000000000001'/s",
            $contents,
            'OnboardingController must keep the `defaultWorkspaceId() ?? '
            ."'a0000000-...001'` fallback for true first-boot. Without it "
            .'the first ever user signup crashes before they get a '
            .'workspace assigned.',
        );
    }
}
