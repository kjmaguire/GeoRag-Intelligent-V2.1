<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * The architecture doc must not name a database table that does not exist.
 *
 * CLAUDE.md designates georag-architecture.html the architecture source of
 * truth, which is exactly what makes a phantom table in it expensive: it is
 * the file an engineer reads BEFORE looking at the schema, so a wrong name
 * there is trusted and propagates.
 *
 * The instance that prompted this test survived for months. The doc carried a
 * data-dictionary row for `silver.vector_features` -- a "polymorphic landing
 * for generic geological vector features" -- described in enough detail to be
 * convincing, sitting one row below the real `silver.spatial_features` entry.
 * No migration has ever created it. Two more places in the same file routed
 * Shapefile and GeoPackage ingest into it, so a reader had three mutually
 * consistent mentions and no reason to doubt any of them.
 *
 * This checks names, not columns. Column-level drift is real too, but a name
 * that resolves to nothing is the failure that sends someone writing a query
 * against a table that cannot be created.
 */
final class ArchitectureDocSchemaParityTest extends TestCase
{
    /** Schemas whose tables are created by migrations in this repo. */
    private const SCHEMAS = ['bronze', 'silver', 'gold', 'audit', 'eval'];

    /**
     * Names that appear in the doc but are deliberately not in a migration,
     * each with the reason. Empty is the healthy state.
     *
     * @var array<string, string>
     */
    private const EXPECTED_ABSENT = [];

    private function repoRoot(): string
    {
        return dirname(__DIR__, 2);
    }

    /**
     * Every `schema.table` the doc names inside a <code> tag.
     *
     * Scoped to <code> deliberately. Prose mentions a table name in passing
     * ("the old vector_features idea") and flagging those would push authors
     * toward vaguer prose; a name marked up as code is a claim about the
     * schema.
     *
     * @return list<string>
     */
    private function tablesNamedInDoc(): array
    {
        $doc = file_get_contents($this->repoRoot().'/georag-architecture.html');
        self::assertNotFalse($doc, 'georag-architecture.html is unreadable');

        $schemas = implode('|', self::SCHEMAS);
        preg_match_all("#<code>(({$schemas})\.[a-z0-9_]+)</code>#", $doc, $m);

        $names = array_values(array_unique($m[1]));
        sort($names);

        return $names;
    }

    /** Every migration and raw-SQL file, concatenated. */
    private function schemaSource(): string
    {
        $source = '';
        $dir = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($this->repoRoot().'/database'),
        );

        foreach ($dir as $file) {
            if (! $file->isFile()) {
                continue;
            }
            if (! in_array($file->getExtension(), ['php', 'sql'], true)) {
                continue;
            }
            $source .= file_get_contents($file->getPathname());
        }

        return $source;
    }

    #[Test]
    public function the_scan_finds_the_tables_it_is_supposed_to_check(): void
    {
        // Guards the guard. If the regex stops matching -- the doc is
        // reformatted, <code> becomes a <span> -- every assertion below
        // passes vacuously and the check silently stops existing.
        $named = $this->tablesNamedInDoc();

        self::assertGreaterThanOrEqual(
            20,
            count($named),
            'Only '.count($named).' schema-qualified table names were found in '
            .'georag-architecture.html. The doc used to name 26; the scan is '
            .'probably broken rather than the doc emptied.',
        );
        self::assertContains('silver.spatial_features', $named);
    }

    #[Test]
    public function every_table_the_doc_names_exists_in_a_migration(): void
    {
        $schema = $this->schemaSource();

        $missing = [];
        foreach ($this->tablesNamedInDoc() as $name) {
            if (array_key_exists($name, self::EXPECTED_ABSENT)) {
                continue;
            }
            if (! str_contains($schema, $name)) {
                $missing[] = $name;
            }
        }

        self::assertSame(
            [],
            $missing,
            'georag-architecture.html names tables that no migration or raw '
            ."SQL file creates:\n  ".implode("\n  ", $missing)."\n\n"
            .'Either the table was renamed and the doc was not updated, or '
            .'the doc describes something nobody built. Fix the doc, or -- if '
            .'the name is genuinely provisioned outside this repo -- record it '
            .'in EXPECTED_ABSENT with the reason.',
        );
    }

    #[Test]
    public function the_absent_list_has_not_gone_stale(): void
    {
        // An entry that IS now in a migration means the exemption outlived
        // its reason, and a stale exemption is how the next phantom hides.
        $schema = $this->schemaSource();

        $stale = [];
        foreach (array_keys(self::EXPECTED_ABSENT) as $name) {
            if (str_contains($schema, $name)) {
                $stale[] = $name;
            }
        }

        // Asserted outside the loop so an empty exemption list -- the healthy
        // state -- still counts as a real check rather than a risky test.
        self::assertSame(
            [],
            $stale,
            'These names are exempted in EXPECTED_ABSENT but a migration now '
            .'creates them, so the exemption outlived its reason:
  '
            .implode('
  ', $stale),
        );
    }
}
