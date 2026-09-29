<?php

namespace Tests\Unit\Jobs;

use App\Jobs\StreamQueryFromFastApi;

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
