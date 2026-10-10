<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * database/raw/phase0/95-rls-policies.sql must not re-create `tenant_isolation`
 * on a table whose policy the migration chain owns.
 *
 * The ECS migrate task runs `php artisan migrate` THEN `php artisan
 * db:apply-raw`, so a raw file always has the last word. 95's DO-block DROPs
 * `tenant_isolation` on a hard-coded list of tables and re-creates it in the
 * fail-open shape (`... OR current_setting('app.workspace_id', true) IS NULL`),
 * which silently undid 2026_08_14_030000 / 2026_08_17_090000 / 2026_08_28_100000 /
 * 2026_08_28_100100 on every deploy (database audit 2026-10).
 *
 * What the real database looks like after both layers is checked by
 * src/fastapi/tests/test_rls_after_raw.py, in the CI job that builds it the
 * production way. This is the cheap half: it reads the file, needs no database,
 * and fails in the SQLite suite on the line that re-adds a table.
 *
 * Two lists, deliberately literal (a typo in either place is then a diff):
 *
 *  - OWNED_BY_MIGRATIONS: fail-closed by the migration chain; 95 must not name
 *    them.
 *  - HELD_OPEN_PENDING_BINDING: the rest of 2026_08_14_030000's verified subset
 *    that 95 still names. A known gap, not a decision: tightening them stops
 *    cost metering until three unbound code paths bind a workspace (see 95).
 *    The set is compared for EQUALITY, so it can only shrink: fixing those
 *    paths and removing the two tables from 95 fails this test until the
 *    constant is updated, and a new table sneaking back into 95 fails it
 *    immediately.
 */
final class RawRlsReopensNothingTest extends TestCase
{
    /** @var list<string> */
    private const OWNED_BY_MIGRATIONS = [
        'workspace.workspace_memberships',
        'workspace.workspace_agent_config',
        'workspace.dry_run_outputs',
        'outbox.pending_propagations',
        'outbox.propagation_attempts',
    ];

    /** @var list<string> */
    private const HELD_OPEN_PENDING_BINDING = [
        'usage.usage_events',
        'usage.workspace_cost_ceilings',
    ];

    #[Test]
    public function raw_95_does_not_recreate_a_policy_on_a_table_the_migrations_close(): void
    {
        $targets = self::macroTargets(self::raw95());

        $reopened = array_values(array_intersect(self::OWNED_BY_MIGRATIONS, $targets));

        $this->assertSame(
            [],
            $reopened,
            "database/raw/phase0/95-rls-policies.sql DROPs and re-creates tenant_isolation, in the fail-open shape, on:\n  "
            .implode("\n  ", $reopened)
            ."\nThe migration chain makes these fail-closed and db:apply-raw runs after it, so this file would undo it on every deploy.",
        );
    }

    #[Test]
    public function the_only_verified_subset_tables_95_still_touches_are_the_documented_ones(): void
    {
        $targets = self::macroTargets(self::raw95());
        $subset = self::verifiedSubset();

        $this->assertNotEmpty($subset, 'could not read the verified subset out of 2026_08_14_030000');

        $stillTouched = array_values(array_intersect($subset, $targets));
        sort($stillTouched);

        $expected = self::HELD_OPEN_PENDING_BINDING;
        sort($expected);

        $this->assertSame(
            $expected,
            $stillTouched,
            'The set of 2026_08_14_030000 tables that 95 still re-creates changed. A table added here is a regression '
            .'(95 would re-open it). A table removed is the fix landing -- update HELD_OPEN_PENDING_BINDING in this test.',
        );
    }

    #[Test]
    public function the_parser_reads_the_real_file(): void
    {
        $targets = self::macroTargets(self::raw95());

        // Tables 95 genuinely manages, so a parser that silently returns [] (and
        // passes both tests above) is caught.
        foreach (['workspace.idempotency_keys', 'audit.audit_ledger', 'workflow.workflow_runs', 'usage.usage_events'] as $expected) {
            $this->assertContains($expected, $targets);
        }
    }

    #[Test]
    public function the_parser_catches_a_table_put_back(): void
    {
        $sql = <<<'SQL'
            DO $$
            DECLARE
                target_tables text[][] := ARRAY[
                    -- ARRAY['workspace', 'dry_run_outputs'] is a comment and does not count
                    ARRAY['workspace', 'workspace_memberships'],
                    ARRAY['audit',     'audit_ledger']
                ];
            BEGIN
            END $$;
            SQL;

        $this->assertSame(
            ['workspace.workspace_memberships', 'audit.audit_ledger'],
            self::macroTargets($sql),
        );
    }

    private static function raw95(): string
    {
        $path = dirname(__DIR__, 2).'/database/raw/phase0/95-rls-policies.sql';
        $sql = file_get_contents($path);
        self::assertIsString($sql, "cannot read {$path}");

        return $sql;
    }

    /**
     * Tables in the DO-block's `target_tables` array, as "schema.table".
     *
     * @return list<string>
     */
    private static function macroTargets(string $sql): array
    {
        $sql = (string) preg_replace('/--[^\n]*/', '', $sql);

        if (preg_match('/target_tables\s+text\[\]\[\]\s*:=\s*ARRAY\[(.*?)\]\s*;/s', $sql, $block) !== 1) {
            return [];
        }

        preg_match_all("/ARRAY\\[\\s*'([a-z_0-9]+)'\\s*,\\s*'([a-z_0-9]+)'\\s*\\]/i", $block[1], $pairs, PREG_SET_ORDER);

        return array_map(static fn (array $m): string => strtolower($m[1].'.'.$m[2]), $pairs);
    }

    /**
     * (schema.table) pairs in 2026_08_14_030000's TABLES constant.
     *
     * @return list<string>
     */
    private static function verifiedSubset(): array
    {
        $path = dirname(__DIR__, 2).'/database/migrations/2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset.php';
        $source = file_get_contents($path);
        self::assertIsString($source, "cannot read {$path}");

        preg_match_all("/'schema'\\s*=>\\s*'([a-z_0-9]+)'\\s*,\\s*'table'\\s*=>\\s*'([a-z_0-9]+)'/", $source, $pairs, PREG_SET_ORDER);

        return array_map(static fn (array $m): string => $m[1].'.'.$m[2], $pairs);
    }
}
