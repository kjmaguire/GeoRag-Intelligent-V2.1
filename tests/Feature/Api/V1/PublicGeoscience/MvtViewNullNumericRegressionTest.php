<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use Illuminate\Foundation\Testing\DatabaseTransactions;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Regression test for the null-numeric MVT view bug that silently broke
 * drillhole + resource-potential tile parsing in mid-April 2026.
 *
 * Failure mode: when a `v_pg_*_mvt` view exposes a column of a declared
 * numeric / boolean / date / timestamp type, and any row in the underlying
 * canonical table has NULL in that column, Martin encodes the MVT feature
 * with a null value in that property. MapLibre's tile-parsing worker
 * rejects the entire tile with
 *     "Expected value to be of type number, but found null instead"
 * and no circles / polygons / lines render for that tile. Clicks appear to
 * do nothing because queryRenderedFeatures returns empty.
 *
 * Convention (see docs/mvt-nullable-numeric-convention.md):
 *   - Pattern A: drop the column from the MVT view if it's always-null.
 *   - Pattern B: COALESCE the column to a safe sentinel + emit a paired
 *     `has_*` bool so the client can distinguish "zero recorded" from
 *     "no data".
 *
 * This test walks every `v_pg_%_mvt` view in `public_geoscience` and fails
 * if any non-text column contains NULL values. Either remedy (drop the
 * column or COALESCE it) will pass the test; both are valid.
 *
 * Run with:
 *     php artisan test --filter MvtViewNullNumericRegressionTest
 */
class MvtViewNullNumericRegressionTest extends TestCase
{
    use DatabaseTransactions;

    protected function setUp(): void
    {
        parent::setUp();

        // Queries information_schema.columns and public_geoscience.v_pg_* MVT
        // views directly — both are Postgres-only. Skip under the SQLite test
        // fixture; run the regression against a real Postgres test connection.
        $this->skipIfSqlite();
    }

    public function test_no_mvt_view_exposes_nulls_in_typed_columns(): void
    {
        $offenders = [];

        // Enumerate every non-text column across every MVT view.
        $columns = DB::select("
            SELECT table_name, column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public_geoscience'
               AND table_name LIKE 'v_pg_%_mvt'
               AND is_nullable = 'YES'
               AND data_type NOT IN (
                     'character varying', 'text', 'USER-DEFINED',
                     'ARRAY', 'uuid'
                 )
             ORDER BY table_name, ordinal_position
        ");

        foreach ($columns as $col) {
            // Count NULLs in the view (so the COALESCE in the view is
            // exercised, not the underlying table). We use a prepared-safe
            // column/view name by whitelisting the set above.
            $tableName = $col->table_name;
            $columnName = $col->column_name;
            $sql = sprintf(
                'SELECT COUNT(*) AS nulls FROM public_geoscience.%s WHERE %s IS NULL',
                $tableName,
                $columnName,
            );
            $result = DB::selectOne($sql);
            if ((int) $result->nulls > 0) {
                $offenders[] = sprintf(
                    '%s.%s (%s, nullable=%s) — %d null rows',
                    $tableName,
                    $columnName,
                    $col->data_type,
                    $col->is_nullable,
                    (int) $result->nulls,
                );
            }
        }

        $this->assertEmpty(
            $offenders,
            "One or more MVT views expose non-text columns with NULL rows. "
            . "Martin will encode these as null-typed properties, which breaks "
            . "MapLibre's tile parser silently. Fix with Pattern A (drop the "
            . "column) or Pattern B (COALESCE + has_* bool) — see "
            . "docs/mvt-nullable-numeric-convention.md.\n\nOffenders:\n  - "
            . implode("\n  - ", $offenders)
        );
    }

    /**
     * Spot-checks Pattern B is wired correctly for the two columns that
     * needed it at convention-establishment time. If someone drops the
     * paired `has_*` bool without updating the popup code, this fails.
     *
     * If future refactoring removes these columns entirely (e.g., because
     * all rows got filled in), delete the corresponding assertion instead
     * of weakening this test.
     */
    public function test_known_pattern_b_pairs_exist(): void
    {
        $pairs = [
            'v_pg_drillhole_collars_mvt' => [
                'total_length_m'   => 'numeric',
                'has_total_length' => 'boolean',
            ],
            'v_pg_resource_potential_mvt' => [
                'potential_rank'     => 'smallint',
                'has_potential_rank' => 'boolean',
            ],
        ];

        foreach ($pairs as $view => $expectedCols) {
            foreach ($expectedCols as $col => $expectedType) {
                $row = DB::selectOne("
                    SELECT data_type
                      FROM information_schema.columns
                     WHERE table_schema = 'public_geoscience'
                       AND table_name   = ?
                       AND column_name  = ?
                ", [$view, $col]);

                $this->assertNotNull(
                    $row,
                    "MVT view $view is missing column $col — Pattern B pairing "
                    . "(see docs/mvt-nullable-numeric-convention.md) appears to "
                    . "have been broken."
                );

                $this->assertSame(
                    $expectedType,
                    $row->data_type,
                    "MVT view $view.$col expected type $expectedType, got "
                    . "{$row->data_type}. If the canonical column type changed, "
                    . "update this test + the Martin yaml declaration."
                );
            }
        }
    }
}
