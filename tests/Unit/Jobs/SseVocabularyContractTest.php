<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * CHAT-22 — the SSE vocabulary is a contract in three places: the FastAPI
 * producer, this repo's relay job, and the React consumer. Change it in one
 * and the chat breaks silently, usually as a stream that never terminates
 * because the browser waits for a terminal frame it will never see.
 *
 * The three had drifted: queries.py's docstring omitted `status` and
 * `bind`, the job's docblock omitted `bind`, and Chat.tsx still listed the
 * long-dead `routing`. Each now carries the same canonical line, and this
 * test fails when one of them changes without the others.
 */
final class SseVocabularyContractTest extends TestCase
{
    private const CANONICAL = 'SSE vocabulary: status · bind · delta · citation · completed · failed';

    /** @var list<string> */
    private const NAMES = ['status', 'bind', 'delta', 'citation', 'completed', 'failed'];

    /**
     * @return array<string, array{0: string}>
     */
    public static function contractFiles(): array
    {
        return [
            'FastAPI producer' => ['src/fastapi/app/routers/queries.py'],
            'Laravel relay' => ['app/Jobs/StreamQueryFromFastApi.php'],
            'React consumer' => ['resources/js/Pages/Foundry/Chat.tsx'],
            'React helpers' => ['resources/js/lib/chatStream.ts'],
        ];
    }

    #[Test]
    #[DataProvider('contractFiles')]
    public function every_side_declares_the_same_six_names(string $path): void
    {
        $source = (string) file_get_contents(base_path($path));

        $this->assertStringContainsString(self::CANONICAL, $source, "$path no longer carries the canonical SSE vocabulary line");
        $this->assertStringNotContainsString(' / routing / ', $source, "$path lists the dead `routing` frame");
    }

    #[Test]
    public function the_typed_list_matches_the_canonical_line(): void
    {
        $helpers = (string) file_get_contents(base_path('resources/js/lib/chatStream.ts'));
        $quoted = implode(', ', array_map(fn (string $n): string => "'$n'", self::NAMES));

        $this->assertStringContainsString("SSE_VOCABULARY = [$quoted] as const", $helpers);
    }

    #[Test]
    public function the_relay_never_emits_a_frame_outside_the_vocabulary(): void
    {
        // `error` was the one out-of-contract name the job produced; the
        // frontend's tolerance for it was the only thing keeping that path
        // from hanging.
        $job = (string) file_get_contents(base_path('app/Jobs/StreamQueryFromFastApi.php'));

        preg_match_all("/new QueryStreamEvent\(\s*\\\$this->channel,\s*'([a-z_]+)'/", $job, $m);
        $this->assertNotEmpty($m[1]);
        foreach ($m[1] as $name) {
            $this->assertContains($name, self::NAMES, "the job broadcasts `$name`, which is not in the SSE vocabulary");
        }
    }

    #[Test]
    public function the_consumer_handles_both_terminal_frames(): void
    {
        $chat = (string) file_get_contents(base_path('resources/js/Pages/Foundry/Chat.tsx'));

        $this->assertStringContainsString("eventType === 'completed'", $chat);
        $this->assertStringContainsString("eventType === 'failed'", $chat);
    }
}
