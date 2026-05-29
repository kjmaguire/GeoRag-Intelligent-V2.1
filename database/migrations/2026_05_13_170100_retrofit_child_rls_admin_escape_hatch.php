<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Doc-phase 133 — retrofit RLS admin escape hatch on EXISTS-based
 * child policies. Sibling to doc-phase 129's retrofit on top-level
 * tables.
 *
 * The doc-phase 129 retrofit noted that EXISTS-based child policies
 * inherit the admin escape hatch transitively through their parent.
 * That's only true when the child policy's EXISTS subquery does NOT
 * re-check the GUC itself — which most of them DO. So when the GUC
 * is unset (admin context):
 *
 *   - Parent RLS sees the GUC-is-empty escape hatch and shows all rows
 *   - Child policy's EXISTS subquery selects from the parent (sees all)
 *     BUT the WHERE clause inside the EXISTS still applies
 *     `parent.workspace_id::text = current_setting('app.workspace_id', true)`
 *   - That comparison is `<uuid_text> = ''` → FALSE
 *   - EXISTS returns FALSE → child row hidden
 *
 * Result: admin queries against child tables (decision_options,
 * decision_evidence_links, decision_outcomes, decision_lessons_learned,
 * hypothesis_evidence_links) return 0 rows.
 *
 * Fix: rewrite each EXISTS to OR-in the empty/null GUC checks, matching
 * the doc-phase 129 pattern. This also fixes the same-tx INSERT case
 * (admin process inserts a child row, then queries it back to verify —
 * if the GUC isn't set, the row is invisible even within the tx).
 *
 * Five child policies retrofitted:
 *   - silver.decision_evidence_links  (doc-phase 92)
 *   - silver.decision_options          (doc-phase 92)
 *   - silver.decision_outcomes         (doc-phase 92)
 *   - silver.decision_lessons_learned  (doc-phase 92)
 *   - silver.hypothesis_evidence_links (doc-phase 91)
 */
return new class extends Migration
{
    /**
     * @var array<int, array{table: string, policy: string, parent_table: string, parent_alias: string}>
     */
    private array $policies = [
        [
            'table' => 'silver.decision_evidence_links',
            'policy' => 'decision_evidence_links_workspace_isolation',
            'parent_table' => 'silver.decision_records',
            'parent_alias' => 'd',
        ],
        [
            'table' => 'silver.decision_options',
            'policy' => 'decision_options_workspace_isolation',
            'parent_table' => 'silver.decision_records',
            'parent_alias' => 'd',
        ],
        [
            'table' => 'silver.decision_outcomes',
            'policy' => 'decision_outcomes_workspace_isolation',
            'parent_table' => 'silver.decision_records',
            'parent_alias' => 'd',
        ],
        [
            'table' => 'silver.decision_lessons_learned',
            'policy' => 'decision_lessons_learned_workspace_isolation',
            'parent_table' => 'silver.decision_records',
            'parent_alias' => 'd',
        ],
        [
            'table' => 'silver.hypothesis_evidence_links',
            'policy' => 'hypothesis_evidence_links_workspace_isolation',
            'parent_table' => 'silver.hypotheses',
            'parent_alias' => 'h',
        ],
    ];

    public function up(): void
    {
        // Doc-phase 157 — RLS policies are PG-only. Skip under sqlite.
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        foreach ($this->policies as $p) {
            // Drop the strict child policy.
            DB::statement(sprintf(
                'DROP POLICY IF EXISTS %s ON %s',
                $p['policy'],
                $p['table'],
            ));

            // The child PK column matches the parent PK by name (decision_id,
            // hypothesis_id). The child row references the parent's PK column
            // of the same name.
            $childTable = $p['table'];
            $parentTable = $p['parent_table'];
            $alias = $p['parent_alias'];
            $parentPkColumn = match ($parentTable) {
                'silver.decision_records' => 'decision_id',
                'silver.hypotheses' => 'hypothesis_id',
                default => 'id',
            };

            DB::statement(sprintf(<<<'SQL'
                CREATE POLICY %s
                    ON %s
                    USING (
                        EXISTS (
                            SELECT 1 FROM %s %s
                            WHERE %s.%s = %s.%s
                              AND (
                                  (%s.workspace_id::text = current_setting('app.workspace_id', true))
                                  OR current_setting('app.workspace_id', true) IS NULL
                                  OR current_setting('app.workspace_id', true) = ''
                              )
                        )
                    )
                SQL,
                $p['policy'],
                $childTable,
                $parentTable, $alias,
                $alias, $parentPkColumn, $childTable, $parentPkColumn,
                $alias,
            ));
        }
    }

    public function down(): void
    {
        // Revert to strict child policies (no escape hatch).
        foreach ($this->policies as $p) {
            DB::statement(sprintf(
                'DROP POLICY IF EXISTS %s ON %s',
                $p['policy'],
                $p['table'],
            ));
            $childTable = $p['table'];
            $parentTable = $p['parent_table'];
            $alias = $p['parent_alias'];
            $parentPkColumn = match ($parentTable) {
                'silver.decision_records' => 'decision_id',
                'silver.hypotheses' => 'hypothesis_id',
                default => 'id',
            };
            DB::statement(sprintf(<<<'SQL'
                CREATE POLICY %s
                    ON %s
                    USING (
                        EXISTS (
                            SELECT 1 FROM %s %s
                            WHERE %s.%s = %s.%s
                              AND %s.workspace_id::text = current_setting('app.workspace_id', true)
                        )
                    )
                SQL,
                $p['policy'],
                $childTable,
                $parentTable, $alias,
                $alias, $parentPkColumn, $childTable, $parentPkColumn,
                $alias,
            ));
        }
    }
};
