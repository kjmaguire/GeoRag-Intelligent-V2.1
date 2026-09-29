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
 * against a table that cannot be created. (Code-vs-schema drift is checked
 * separately, against a migrated database, by
 * scripts/ci/check_sql_against_schema.py.)
 *
 * Tightened 2026-09-29 (database audit PG-15). It used to check only five
 * schemas, and "exists" meant the name appeared anywhere in any .php/.sql file
 * under database/ — including database/raw/_archive/, comments, and RLS
 * policy lists. `silver.section_lines` passed only because an archived file
 * mentioned it, and `silver.pdf_text_blocks` would have passed because an RLS
 * migration lists it. Now every application schema is covered, and a name
 * counts only if a migration or a live raw file CREATEs it (or moves/renames
 * a table to it).
 */
final class ArchitectureDocSchemaParityTest extends TestCase
{
    /** Every schema whose tables are created by migrations in this repo. */
    private const SCHEMAS = [
        'audit', 'backups', 'bronze', 'eval', 'gold', 'interpretation', 'ops',
        'outbox', 'public_geo', 'silver', 'targeting', 'usage', 'workflow',
        'workspace',
    ];

    /** database/raw/ directories that are history or scratch, not DDL. */
    private const EXCLUDED_RAW_DIRS = ['_archive', '_adhoc'];

    /**
     * Names that appear in the doc but are deliberately not in a migration,
     * each with the reason. Empty is the healthy state.
     *
     * @var array<string, string>
     */
    private const EXPECTED_ABSENT = [
        // Reverb broadcastAs() event names in the doc's event table, not
        // tables; they share their prefix with the `workspace` schema.
        'workspace.activity' => 'broadcast event name (WorkspaceActivityBroadcast), not a table',
        'workspace.data_updated' => 'broadcast event name (WorkspaceDataUpdated), not a table',
    ];

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

    /**
     * Every `schema.name` a migration or live raw-SQL file creates.
     *
     * Creating means: CREATE TABLE / VIEW / MATERIALIZED VIEW / FUNCTION with
     * a qualified name (identifiers optionally quoted), Schema::create() with
     * a qualified name, or an ALTER TABLE ... SET SCHEMA / RENAME TO that
     * lands a table at the name. Mentions in comments, GRANTs, policies or
     * ALTERs of an existing table do not count — naming is not creating.
     *
     * @return array<string, true>
     */
    private function createdNames(): array
    {
        $created = [];
        $ident = '"?([a-z_][a-z0-9_]*)"?';

        foreach ($this->schemaFiles() as $path) {
            $src = (string) file_get_contents($path);

            $patterns = [
                '/CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'.$ident.'\s*\.\s*'.$ident.'/i',
                '/CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?'.$ident.'\s*\.\s*'.$ident.'/i',
                '/CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+'.$ident.'\s*\.\s*'.$ident.'/i',
                '/(?:Schema::|->)\s*create\(\s*[\'"]([a-z_][a-z0-9_]*)\.([a-z0-9_]+)[\'"]/i',
            ];
            foreach ($patterns as $pattern) {
                preg_match_all($pattern, $src, $m, PREG_SET_ORDER);
                foreach ($m as $hit) {
                    $created[strtolower($hit[1].'.'.$hit[2])] = true;
                }
            }

            // ALTER TABLE [schema.]t SET SCHEMA s  ->  s.t
            preg_match_all(
                '/ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:'.$ident.'\s*\.\s*)?'.$ident.'\s+SET\s+SCHEMA\s+'.$ident.'/i',
                $src,
                $moves,
                PREG_SET_ORDER,
            );
            foreach ($moves as $hit) {
                $created[strtolower($hit[3].'.'.$hit[2])] = true;
            }

            // ALTER TABLE s.t RENAME TO u  ->  s.u
            preg_match_all(
                '/ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?'.$ident.'\s*\.\s*'.$ident.'\s+RENAME\s+TO\s+'.$ident.'/i',
                $src,
                $renames,
                PREG_SET_ORDER,
            );
            foreach ($renames as $hit) {
                $created[strtolower($hit[1].'.'.$hit[3])] = true;
            }
        }

        return $created;
    }

    /** @return list<string> migrations + raw SQL, minus archive/scratch. */
    private function schemaFiles(): array
    {
        $files = glob($this->repoRoot().'/database/migrations/*.php') ?: [];

        $raw = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($this->repoRoot().'/database/raw', \FilesystemIterator::SKIP_DOTS),
        );
        foreach ($raw as $file) {
            if (! $file->isFile() || $file->getExtension() !== 'sql') {
                continue;
            }
            $path = str_replace(DIRECTORY_SEPARATOR, '/', $file->getPathname());
            foreach (self::EXCLUDED_RAW_DIRS as $skip) {
                if (str_contains($path, '/'.$skip.'/')) {
                    continue 2;
                }
            }
            $files[] = $path;
        }

        sort($files);

        return $files;
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
        $created = $this->createdNames();

        $missing = [];
        foreach ($this->tablesNamedInDoc() as $name) {
            if (array_key_exists($name, self::EXPECTED_ABSENT)) {
                continue;
            }
            if (! isset($created[$name])) {
                $missing[] = $name;
            }
        }

        self::assertSame(
            [],
            $missing,
            'georag-architecture.html names tables that no migration or live raw '
            ."SQL file CREATEs (database/raw/_archive and _adhoc do not count):\n  "
            .implode("\n  ", $missing)."\n\n"
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
        $created = $this->createdNames();

        $stale = [];
        foreach (array_keys(self::EXPECTED_ABSENT) as $name) {
            if (isset($created[$name])) {
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
