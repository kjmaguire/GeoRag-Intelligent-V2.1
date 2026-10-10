<?php

declare(strict_types=1);

namespace Tests\Unit\Support;

use App\Support\AuthorizationAuditLogger;
use Tests\TestCase;

/**
 * The Prometheus export has one `laravel_authz_deny_total{reason=...}` series
 * per entry of AuthorizationAuditLogger::REASONS, because a counter has to be
 * read by name. The list it replaced was kept by hand and had drifted both
 * ways: it omitted `not_project_owner` (written on every owner-only denial) and
 * named four reasons nothing writes.
 *
 * This holds the list to the call sites: every literal `reason:` handed to
 * AuthorizationAuditLogger::deny() anywhere in app/ must be in it, and every
 * entry must be written somewhere.
 */
final class AuthorizationAuditLoggerReasonsTest extends TestCase
{
    /** @return array<string, list<string>> reason => files that write it */
    private function reasonsWritten(): array
    {
        $found = [];
        $iterator = new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator(app_path(), \FilesystemIterator::SKIP_DOTS));

        foreach ($iterator as $file) {
            if (! $file->isFile() || $file->getExtension() !== 'php') {
                continue;
            }
            $source = (string) file_get_contents($file->getPathname());
            if (! str_contains($source, 'AuthorizationAuditLogger::deny(')) {
                continue;
            }

            // Each deny() call up to its closing `);`, so a `reason:` elsewhere
            // in the file is not mistaken for one.
            preg_match_all("/AuthorizationAuditLogger::deny\\([^;]*?reason:\\s*'([^']+)'/s", $source, $matches);
            foreach ($matches[1] as $reason) {
                $found[$reason][] = str_replace(base_path().'/', '', $file->getPathname());
            }
        }

        return $found;
    }

    public function test_every_reason_written_is_in_the_exported_list(): void
    {
        $written = $this->reasonsWritten();

        $this->assertNotEmpty($written, 'the scan found no deny() call at all -- the pattern broke');
        foreach ($written as $reason => $files) {
            $this->assertContains(
                $reason,
                AuthorizationAuditLogger::REASONS,
                "'{$reason}' is written by ".implode(', ', array_unique($files)).' but is not in AuthorizationAuditLogger::REASONS, so its counter is never exported',
            );
        }
    }

    public function test_every_exported_reason_is_written_somewhere(): void
    {
        $written = array_keys($this->reasonsWritten());

        foreach (AuthorizationAuditLogger::REASONS as $reason) {
            $this->assertContains($reason, $written, "'{$reason}' is exported but no call site writes it");
        }
    }

    public function test_the_constants_and_the_list_agree(): void
    {
        $this->assertSame(
            [AuthorizationAuditLogger::REASON_NO_PIVOT_ROW, AuthorizationAuditLogger::REASON_NOT_PROJECT_OWNER],
            AuthorizationAuditLogger::REASONS,
        );
        $this->assertSame(['no_pivot_row', 'not_project_owner'], AuthorizationAuditLogger::REASONS);
    }
}
