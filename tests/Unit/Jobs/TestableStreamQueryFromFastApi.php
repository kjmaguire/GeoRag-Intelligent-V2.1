<?php

namespace Tests\Unit\Jobs;

use App\Jobs\StreamQueryFromFastApi;
use App\Models\QueryAuditLog;

/**
 * Test double: overrides openHttpStream/responseHeaders so the job's
 * fgets() line-reader drains a fake SSE body and sees a fake status line.
 * Everything ELSE in handle() runs for real (JWT mint is fine — the test
 * secret is >= 32 bytes; audit-row updates are guarded by `where first()`
 * returning null in a clean DB).
 */
class TestableStreamQueryFromFastApi extends StreamQueryFromFastApi
{
    public string $fakeSseBody = '';

    public int $fakeStatus = 200;

    /**
     * The JSON body the job would have POSTed to FastAPI, decoded — so a
     * test can assert what the job sends (e.g. the multi-turn `history`).
     *
     * @var array<string, mixed>|null
     */
    public ?array $sentPayload = null;

    /** Read the audit row from the database instead of stubbing it. */
    public bool $useRealAuditLookup = false;

    /** Row returned by lookupAuditRow() when stubbing; a stub with a user_id when null. */
    public ?QueryAuditLog $auditRow = null;

    /** Simulate a query_id with no audit row. */
    public bool $auditRowMissing = false;

    /** Make the audit-row lookup throw, as it does when the DB is down. */
    public bool $auditLookupThrows = false;

    /** Workspace returned by lookupWorkspaceId(); null simulates a project with no workspace. */
    public ?string $workspaceId = '99999999-0000-0000-0000-000000000001';

    /** Make the workspace lookup throw. */
    public bool $workspaceLookupThrows = false;

    protected function lookupAuditRow(): ?QueryAuditLog
    {
        if ($this->auditLookupThrows) {
            throw new \RuntimeException('SQLSTATE[08006] connection refused');
        }

        if ($this->auditRowMissing) {
            return null;
        }

        if ($this->useRealAuditLookup) {
            return parent::lookupAuditRow();
        }

        return $this->auditRow ?? (new QueryAuditLog)->forceFill(['user_id' => 1]);
    }

    protected function lookupWorkspaceId(): ?string
    {
        if ($this->workspaceLookupThrows) {
            throw new \RuntimeException('SQLSTATE[08006] connection refused');
        }

        return $this->workspaceId;
    }

    protected function openHttpStream(string $url, $context): array
    {
        $options = stream_context_get_options($context);
        $content = $options['http']['content'] ?? null;
        $this->sentPayload = is_string($content) ? json_decode($content, true) : null;

        // php://memory stream pre-populated with the canned body. Rewind
        // so the job's fgets() reads from the beginning.
        $stream = fopen('php://memory', 'r+');
        fwrite($stream, $this->fakeSseBody);
        rewind($stream);

        // Real fopen+HTTP would also populate $http_response_header;
        // tests inject via responseHeaders() so we return [] here.
        return [$stream, []];
    }

    protected function responseHeaders(?array $magic): array
    {
        // Canned status line matching the usual HTTP/1.1 NNN X format so
        // the job's preg_match on the first header captures $fakeStatus.
        $phrase = match (true) {
            $this->fakeStatus >= 200 && $this->fakeStatus < 300 => 'OK',
            default => 'Service Unavailable',
        };

        return ["HTTP/1.1 {$this->fakeStatus} {$phrase}"];
    }
}
