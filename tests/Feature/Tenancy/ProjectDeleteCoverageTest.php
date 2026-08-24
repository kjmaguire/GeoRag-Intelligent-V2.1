<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Locks in the invariant a 2026-08-24 audit found broken: every table that
 * carries project-scoped data must be reachable by project deletion.
 *
 * Two independent failure modes were found, and this test covers both.
 *
 * 1. ORPHANS. Roughly a dozen tables carry a `project_id` column with no
 *    foreign key at all, so neither the CASCADE from `silver.projects` nor
 *    `ProjectController::destroy`'s hand-maintained list can reach them. The
 *    rows survive the delete holding real content — `audit.query_audit_log`
 *    keeps every question, answer and client IP; `chat_conversations` /
 *    `chat_messages` keep whole transcripts and continue to appear in the
 *    sidebar pointing at a project that no longer exists.
 *
 * 2. BLOCKERS. Thirteen drillhole child tables referenced `silver.collars`
 *    with no `ON DELETE` clause (NO ACTION), which blocks the parent delete
 *    instead of following it — the whole transaction rolls back and NOTHING
 *    is deleted. Fixed by 2026_08_24_060000; this test stops it returning.
 *
 * The root cause is not any individual missing table: it is that the list
 * lives in two hand-maintained copies (ProjectController::destroy and
 * 2026_08_17_040000_grant_delete_on_project_cleanup_tables_to_georag_app)
 * with nothing checking either against the schema. A table added next month
 * joins neither. This test is that check.
 *
 * Skipped on SQLite — the test-DB parity migration
 * (2026_06_29_020000_provision_project_delete_tables_for_test_db) stubs the
 * cleanup tables with no foreign keys at all, so there is nothing to assert.
 */
final class ProjectDeleteCoverageTest extends TestCase
{
    use RefreshDatabase;

    /**
     * Tables carrying a `project_id` that deliberately survive a project delete.
     *
     * Every entry needs a reason. "It was already like that" is not one — if a
     * table holds project content it belongs in the delete path, not here.
     *
     * @var array<string, string>
     */
    private const SURVIVES_DELETE = [
        // An audit log that a user can erase by deleting the project is not an
        // audit log. Retention is time-based instead, via
        // src/fastapi/app/hatchet_workflows/retention_sweep.py
        // (QUERY_AUDIT_RETENTION_DAYS, default 180).
        'audit.query_audit_log' => 'audit retention is deliberate; time-based purge only',

        // The project row itself, not a child of one.
        'silver.projects' => 'the parent being deleted',

        // Still ON DELETE SET NULL. Nothing deletes it today, so converting it
        // to CASCADE in 2026_08_24_060100 would have been a new behaviour
        // decision rather than enforcement of an existing one — unlike the five
        // tables converted there, which ProjectController::destroy already
        // deleted explicitly. Zero rows in production as of 2026-08-24.
        // DECISION OWED: should radiometric age data outlive its project?
        'silver.geochronology_samples' => 'SET NULL retained pending a deliberate decision',
    ];

    /**
     * Schemas that never hold project-scoped user content.
     *
     * @var list<string>
     */
    private const IGNORED_SCHEMAS = [
        'pg_catalog', 'information_schema', 'partman', 'pgivm', 'topology',
    ];

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('Foreign key actions are a Postgres concern.');
        }
    }

    public function test_every_project_id_column_is_reachable_by_project_deletion(): void
    {
        /** @var list<object{tbl: string}> $rows */
        $rows = DB::select(<<<'SQL'
            SELECT n.nspname || '.' || c.relname AS tbl
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attribute a
                ON a.attrelid = c.oid AND a.attname = 'project_id' AND a.attnum > 0
             WHERE c.relkind IN ('r', 'p')
               AND NOT c.relispartition
               AND n.nspname <> ALL (?)
               -- Reachable if a FK on this table points at silver.projects and cascades.
               AND NOT EXISTS (
                     SELECT 1
                       FROM pg_constraint con
                       JOIN pg_class pc ON pc.oid = con.confrelid
                       JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                      WHERE con.conrelid = c.oid
                        AND con.contype = 'f'
                        AND con.confdeltype = 'c'
                        AND pn.nspname = 'silver' AND pc.relname = 'projects'
                   )
             ORDER BY 1
        SQL, ['{'.implode(',', self::IGNORED_SCHEMAS).'}']);

        $unreachable = array_values(array_diff(
            array_map(static fn (object $r): string => $r->tbl, $rows),
            array_merge(array_keys(self::SURVIVES_DELETE), $this->controllerCleanupTables()),
        ));

        $this->assertSame(
            [],
            $unreachable,
            'Tables carrying project_id that a project delete cannot reach. Each one keeps its '
            .'rows — and its content — after the user believes the project is gone. Fix by adding '
            .'`->constrained()->cascadeOnDelete()` (preferred: it cannot rot), or by adding the '
            ."table to ProjectController::destroy's cleanup list AND the grant list in "
            .'2026_08_17_040000, or by recording a deliberate exemption with its reason in '
            .self::class.'::SURVIVES_DELETE.',
        );
    }

    public function test_no_project_scoped_foreign_key_can_block_a_delete(): void
    {
        /** @var list<object{child: string, conname: string, parent: string}> $blocking */
        $blocking = DB::select(<<<'SQL'
            SELECT con.conrelid::regclass::text  AS child,
                   con.conname                   AS conname,
                   con.confrelid::regclass::text  AS parent
              FROM pg_constraint con
              JOIN pg_class pc ON pc.oid = con.confrelid
              JOIN pg_namespace pn ON pn.oid = pc.relnamespace
             WHERE con.contype = 'f'
               AND con.confdeltype IN ('a', 'r')   -- NO ACTION | RESTRICT
               AND pn.nspname || '.' || pc.relname IN
                   ('silver.projects', 'silver.collars', 'silver.campaigns')
             ORDER BY 1, 2
        SQL);

        $names = array_map(
            static fn (object $r): string => "{$r->child}.{$r->conname} -> {$r->parent}",
            $blocking,
        );

        $this->assertSame(
            [],
            $names,
            'Foreign keys onto the project delete chain that BLOCK instead of cascade. A single '
            .'child row makes ProjectController::destroy roll back its whole transaction, so the '
            .'user is told the delete failed and every row survives — including the ones already '
            .'deleted inside the transaction. Add ON DELETE CASCADE (see migration '
            .'2026_08_24_060000_cascade_project_scoped_foreign_keys).',
        );
    }

    /**
     * The cleanup list ProjectController::destroy actually runs.
     *
     * Read out of the controller source rather than duplicated here: a third
     * hand-maintained copy would be the very problem this test exists to catch.
     *
     * @return list<string>
     */
    private function controllerCleanupTables(): array
    {
        $source = file_get_contents(app_path('Http/Controllers/Api/V1/ProjectController.php'));
        $this->assertIsString($source, 'ProjectController.php is unreadable.');

        // The list is a $tables = [ ... ] array of 'schema.table' string literals.
        preg_match('/\$tables\s*=\s*\[(.*?)\];/s', $source, $block);
        $this->assertNotEmpty(
            $block,
            'Could not find the $tables cleanup array in ProjectController::destroy. If it was '
            .'renamed or restructured, update this test — do not delete it.',
        );

        preg_match_all("/'([a-z_]+\.[a-z_]+)'/", $block[1], $found);

        // silver.projects itself is the row being deleted, not a child of it.
        return array_values(array_diff($found[1], ['silver.projects']));
    }
}
