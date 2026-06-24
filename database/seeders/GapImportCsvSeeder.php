<?php

declare(strict_types=1);

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Ramsey\Uuid\Uuid;
use RuntimeException;

/**
 * Imports the 2026-06-01 ChatGPT gap-question CSV pair into
 * eval.golden_questions:
 *
 *   - tests/golden_questions/csv_imports/questions_500_export.csv
 *       500 per-project descriptive Qs (project named per row)
 *       → question_set = 'gap_import_single_project'
 *
 *   - tests/golden_questions/csv_imports/gap_questions_1000.csv
 *       1000 cross-project A-vs-B comparison Qs (project = 'Cross-project')
 *       → question_set = 'gap_import_cross_project'
 *
 * Schema mapping:
 *   CSV.question          → question_text
 *   CSV.project           → context_setup.project_scope
 *   CSV.expected_keywords → expected_entities (JSONB array of
 *                            {"type":"keyword","value":"<kw>"} objects,
 *                            split on ';' and trimmed)
 *
 * Each row gets a deterministic UUID v5 keyed off
 * "<source_file_key>:row<N>" so re-runs are idempotent — same source
 * row always upserts to the same question_id.
 *
 * Run with:
 *     php artisan db:seed --class=GapImportCsvSeeder
 *
 * Refs:
 *   - migration 2026_06_01_120000_extend_golden_questions_set_check.php
 *   - existing GoldenQuestionsSeeder.php (YAML cousin)
 */
class GapImportCsvSeeder extends Seeder
{
    private const SEEDER_AUTHOR_USER_ID = 971; // Kyle — kyle@georag.local

    /**
     * Source CSVs. question_set is derived per row from the CSV's
     * `project` column — 'Cross-project' → gap_import_cross_project,
     * any specific project name → gap_import_single_project. The
     * 1000-row file contains a mix (150 cross + 850 single), so
     * file-based bucketing would mis-tag the singles.
     *
     * @var array<int, array{key: string, path: string}>
     */
    private const SOURCES = [
        [
            'key' => 'csv-export-500',
            'path' => 'tests/golden_questions/csv_imports/questions_500_export.csv',
        ],
        [
            'key' => 'csv-gap-1000',
            'path' => 'tests/golden_questions/csv_imports/gap_questions_1000.csv',
        ],
    ];

    private const CROSS_PROJECT_SENTINEL = 'Cross-project';

    public function run(): void
    {
        $totalInserted = 0;
        $totalUpdated = 0;
        $totalSkipped = 0;

        foreach (self::SOURCES as $source) {
            [$inserted, $updated, $skipped] = $this->ingestFile(
                key: $source['key'],
                relativePath: $source['path'],
            );

            $totalInserted += $inserted;
            $totalUpdated += $updated;
            $totalSkipped += $skipped;

            $this->command?->info(sprintf(
                '  %s: %d inserted, %d updated, %d skipped',
                $source['key'],
                $inserted,
                $updated,
                $skipped,
            ));
        }

        $this->command?->info(sprintf(
            'GapImportCsvSeeder complete: %d inserted, %d updated, %d skipped.',
            $totalInserted,
            $totalUpdated,
            $totalSkipped,
        ));
    }

    /**
     * @return array{0:int,1:int,2:int} [inserted, updated, skipped]
     */
    private function ingestFile(string $key, string $relativePath): array
    {
        $absolutePath = base_path($relativePath);

        if (! is_file($absolutePath)) {
            throw new RuntimeException("CSV source not found: {$absolutePath}");
        }

        $handle = fopen($absolutePath, 'r');
        if ($handle === false) {
            throw new RuntimeException("Unable to open CSV: {$absolutePath}");
        }

        $header = fgetcsv($handle);
        if ($header === false) {
            fclose($handle);
            throw new RuntimeException("Empty CSV: {$absolutePath}");
        }

        // Strip UTF-8 BOM from the first column header if present.
        $header[0] = preg_replace('/^\xef\xbb\xbf/', '', (string) $header[0]);

        $expectedColumns = ['question', 'project', 'expected_keywords'];
        if (array_slice($header, 0, 3) !== $expectedColumns) {
            fclose($handle);
            throw new RuntimeException(sprintf(
                'CSV header mismatch in %s. Expected [%s], got [%s].',
                $absolutePath,
                implode(', ', $expectedColumns),
                implode(', ', $header),
            ));
        }

        $inserted = 0;
        $updated = 0;
        $skipped = 0;
        $rowIndex = 0;

        while (($row = fgetcsv($handle)) !== false) {
            $rowIndex++;

            $question = trim((string) ($row[0] ?? ''));
            $project = trim((string) ($row[1] ?? ''));
            $keywordsRaw = (string) ($row[2] ?? '');

            if ($question === '') {
                $skipped++;

                continue;
            }

            $expectedEntities = $this->parseKeywords($keywordsRaw);

            $contextSetup = [
                'source_id' => "{$key}:row{$rowIndex}",
                'source_file' => basename($relativePath),
                'csv_row_index' => $rowIndex,
                'project_scope' => $project,
                'import_batch' => 'chatgpt_gap_2026_06_01',
            ];

            $questionSet = $project === self::CROSS_PROJECT_SENTINEL
                ? 'gap_import_cross_project'
                : 'gap_import_single_project';

            $questionId = Uuid::uuid5(
                Uuid::NAMESPACE_OID,
                $contextSetup['source_id'],
            )->toString();

            $existing = DB::table('eval.golden_questions')
                ->where('question_id', $questionId)
                ->exists();

            DB::table('eval.golden_questions')->updateOrInsert(
                ['question_id' => $questionId],
                [
                    'question_set' => $questionSet,
                    'question_text' => $question,
                    'context_setup' => json_encode($contextSetup, JSON_THROW_ON_ERROR),
                    'expected_intent_class' => null,
                    'expected_citations' => json_encode([], JSON_THROW_ON_ERROR),
                    'expected_entities' => json_encode($expectedEntities, JSON_THROW_ON_ERROR),
                    'expected_numeric_values' => json_encode([], JSON_THROW_ON_ERROR),
                    'expected_refusal' => false,
                    'expected_refusal_reason' => null,
                    'expected_language_compliance' => json_encode([], JSON_THROW_ON_ERROR),
                    'difficulty' => 'medium',
                    'authored_by_user_id' => self::SEEDER_AUTHOR_USER_ID,
                    'status' => 'active',
                ],
            );

            $existing ? $updated++ : $inserted++;
        }

        fclose($handle);

        return [$inserted, $updated, $skipped];
    }

    /**
     * Split the `;`-delimited keyword field into the expected_entities
     * JSONB shape used elsewhere in eval.golden_questions:
     *   [{"type":"keyword","value":"<kw>"}, ...]
     *
     * @return list<array{type:string,value:string}>
     */
    private function parseKeywords(string $raw): array
    {
        if (trim($raw) === '') {
            return [];
        }

        $parts = array_map('trim', explode(';', $raw));
        $parts = array_values(array_filter($parts, static fn (string $p): bool => $p !== ''));

        return array_map(
            static fn (string $kw): array => ['type' => 'keyword', 'value' => $kw],
            $parts,
        );
    }
}
