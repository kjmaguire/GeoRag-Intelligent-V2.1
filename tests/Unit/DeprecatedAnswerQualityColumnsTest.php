<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * `faithfulness_score` and `context_precision_score` have no writer.
 *
 * They are REAL columns on a live Eloquent model with `float` casts, which is
 * exactly what makes them a trap: everything about them looks operational.
 * Their only producer, score_answer_quality.py, was removed in 09d1d35 on
 * 2026-07-27, and the original column comment said "NULL = not yet scored" --
 * so a dashboard querying `WHERE faithfulness_score < 0.5`, finding nothing,
 * reports "no low-faithfulness answers" when the truth is "nothing has been
 * scored since July".
 *
 * This test is a two-way staleness check, not a prohibition:
 *
 *   - While no writer exists, the deprecation notes must be present.
 *   - The day a writer IS restored, this test fails and tells you to take
 *     the notes back out. A stale "DEPRECATED" on a column that has started
 *     working again is the same failure in the other direction.
 */
final class DeprecatedAnswerQualityColumnsTest extends TestCase
{
    private const COLUMNS = ['faithfulness_score', 'context_precision_score'];

    /** Files that legitimately name the columns without writing them. */
    private const NON_WRITERS = [
        'app/Models/QueryAuditLog.php',
        'tests/Unit/DeprecatedAnswerQualityColumnsTest.php',
    ];

    private function repoRoot(): string
    {
        return dirname(__DIR__, 2);
    }

    /**
     * Source files outside the migrations that mention either column.
     *
     * @return list<string>
     */
    private function mentions(): array
    {
        $roots = ['app', 'src/fastapi/app', 'resources/js', 'src/dagster'];
        $found = [];

        foreach ($roots as $rel) {
            $base = $this->repoRoot().'/'.$rel;
            if (! is_dir($base)) {
                continue;
            }

            $files = new \RecursiveIteratorIterator(
                new \RecursiveDirectoryIterator($base),
            );

            foreach ($files as $file) {
                if (! $file->isFile()) {
                    continue;
                }
                if (! in_array($file->getExtension(), ['php', 'py', 'ts', 'tsx'], true)) {
                    continue;
                }

                $body = file_get_contents($file->getPathname());
                foreach (self::COLUMNS as $column) {
                    if (str_contains((string) $body, $column)) {
                        // Normalised on DIRECTORY_SEPARATOR rather than
                        // a literal backslash so the comparison against
                        // NON_WRITERS holds on Windows and Linux alike.
                        $path = str_replace(
                            DIRECTORY_SEPARATOR,
                            '/',
                            substr(
                                $file->getPathname(),
                                strlen($this->repoRoot()) + 1,
                            ),
                        );
                        $found[$path] = true;
                        break;
                    }
                }
            }
        }

        $paths = array_keys($found);
        sort($paths);

        return $paths;
    }

    #[Test]
    public function no_code_writes_the_columns(): void
    {
        $unexpected = array_values(array_diff($this->mentions(), self::NON_WRITERS));

        self::assertSame(
            [],
            $unexpected,
            "These files reference the deprecated answer-quality columns:\n  "
            .implode("\n  ", $unexpected)."\n\n"
            .'If one of them is a NEW WRITER, that is good news -- remove the '
            .'DEPRECATED comments from the 2026_08_22_010000 migration and '
            .'from app/Models/QueryAuditLog.php, and add the file to '
            .'NON_WRITERS only if it reads rather than writes. Leaving a '
            .'DEPRECATED comment on a column that has started working again '
            .'is the same trap pointing the other way.',
        );
    }

    #[Test]
    public function the_deprecation_migration_says_what_null_does_not_mean(): void
    {
        $migration = file_get_contents(
            $this->repoRoot()
            .'/database/migrations/2026_08_22_010000_deprecate_answer_quality_score_columns.php',
        );

        self::assertNotFalse($migration);

        foreach (self::COLUMNS as $column) {
            self::assertStringContainsString($column, (string) $migration);
        }

        // The original comment's "NULL = not yet scored" is the specific
        // sentence that inverted the reading, so the replacement has to
        // contradict it explicitly rather than merely omit it.
        self::assertStringContainsString(
            'NULL does NOT mean',
            (string) $migration,
        );
        self::assertStringContainsString('DEPRECATED', (string) $migration);
    }

    #[Test]
    public function the_model_warns_at_both_places_that_expose_the_columns(): void
    {
        // $fillable and $casts are the two things that make the columns look
        // operational to someone reading the model, so both carry the note.
        $model = (string) file_get_contents(
            $this->repoRoot().'/app/Models/QueryAuditLog.php',
        );

        $position = strpos($model, 'protected $casts');
        self::assertNotFalse($position, 'QueryAuditLog has no $casts');

        $fillableHalf = substr($model, 0, $position);
        $castsHalf = substr($model, $position);

        self::assertStringContainsString('DEPRECATED', $fillableHalf);
        self::assertStringContainsString('no writer since', $castsHalf);
    }
}
