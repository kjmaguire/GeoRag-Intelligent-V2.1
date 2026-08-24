<?php

declare(strict_types=1);

namespace Tests\Unit\Config;

use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Anything .env.example marks REQUIRED must appear in the production
 * template too.
 *
 * `.env.production.example` is the file an operator provisions a new
 * deployment from. Six REQUIRED variables were missing from it on
 * 2026-08-22, and they were not cosmetic:
 *
 *   REVERB_MAX_REQUEST_SIZE / REVERB_APP_MAX_MESSAGE_SIZE — both default to
 *     10 KB, which silently truncates the chat stream's final `completed`
 *     frame. .env.example says so in as many words; this file did not
 *     mention them, so a deployment built from it shipped with the bug the
 *     other file warns about.
 *   HATCHET_CLIENT_TOKEN — without it the worker cannot connect and nothing
 *     ingests at all.
 *   AUDIT_ENCRYPTION_KEY — pgcrypto key for audit PII and the per-flow JWT
 *     registry.
 *   EXTERNAL_NOTIFICATION_HMAC_SECRET — outbound webhook signing.
 *   GEORAG_APP_USER — the non-superuser role the app connects as. Its
 *     PASSWORD was in the template; the username was not.
 *
 * Drift in this direction is silent by construction: the dev file is the one
 * people edit, and nothing reads the production template except a human
 * setting up an environment they cannot test until it is live.
 */
final class ProductionEnvTemplateTest extends TestCase
{
    /**
     * How far above a variable to look for the word REQUIRED.
     *
     * The templates put a comment block above each entry; twelve lines
     * covers the longest of them without reaching the previous variable.
     */
    private const COMMENT_LOOKBACK = 12;

    #[Test]
    public function every_required_variable_is_in_the_production_template(): void
    {
        $prod = array_keys($this->declaredIn(base_path('.env.production.example')));

        $missing = array_values(array_diff($this->requiredInDevTemplate(), $prod));
        sort($missing);

        $this->assertSame([], $missing, implode("\n", array_merge(
            ['Documented REQUIRED in .env.example, absent from .env.production.example:'],
            array_map(static fn (string $k): string => "  {$k}", $missing),
        )));
    }

    #[Test]
    public function the_dev_template_actually_marks_things_required(): void
    {
        // Guards the test above against silently passing because the
        // REQUIRED convention was dropped or the parser stopped matching.
        $this->assertGreaterThan(
            10,
            count($this->requiredInDevTemplate()),
            'no REQUIRED markers found in .env.example — has the convention changed?',
        );
    }

    /** @return list<string> */
    private function requiredInDevTemplate(): array
    {
        $path = base_path('.env.example');
        $lines = $this->linesOf($path);

        $required = [];
        foreach ($this->declaredIn($path) as $key => $lineNumber) {
            $from = max(0, $lineNumber - self::COMMENT_LOOKBACK);
            $context = implode("\n", array_slice($lines, $from, $lineNumber - $from));
            if (str_contains($context, 'REQUIRED')) {
                $required[] = $key;
            }
        }

        return $required;
    }

    /**
     * Uncommented VAR= declarations, mapped to their zero-based line number.
     *
     * @return array<string, int>
     */
    private function declaredIn(string $path): array
    {
        $lines = $this->linesOf($path);

        $declared = [];
        foreach ($lines as $i => $line) {
            if (preg_match('/^([A-Z][A-Z0-9_]{2,})=/', trim($line), $m) === 1) {
                $declared[$m[1]] = $i;
            }
        }

        return $declared;
    }

    /**
     * Lines of a file, split on any newline convention.
     *
     * PHP_EOL is CRLF on Windows and the templates are stored with LF, so
     * exploding on it returns the whole file as a single element and every
     * lookup silently finds nothing.
     *
     * @return list<string>
     */
    private function linesOf(string $path): array
    {
        $lines = preg_split('/\R/', (string) file_get_contents($path));

        return $lines === false ? [] : $lines;
    }
}
