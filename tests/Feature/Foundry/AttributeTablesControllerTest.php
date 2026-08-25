<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Http\Controllers\Foundry\AttributeTablesController;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * AttributeTablesController — the read path for silver.attribute_tables.
 *
 * Postgres-only, and not merely because the schema is: the point of most of
 * these cases is jsonb behaviour (jsonb_object_keys, jsonb_typeof) and the
 * fail-closed RLS policy, none of which SQLite has. Run with:
 *   php artisan test -c phpunit.pgsql.xml tests/Feature/Foundry/AttributeTablesControllerTest.php
 */
final class AttributeTablesControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    /** sha256 of the fixture soil-survey file. Any 64 hex chars will do. */
    private const SOILS_SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90';

    private const LEGEND_SHA = '0f0e0d0c0b0a09080706050403020100f0e0d0c0b0a090807060504030201000';

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
        $slug = 'attr-tables-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Attribute Tables Workspace', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);

        $this->registerRouteUntilWired();
    }

    /**
     * Bootstrap shim, inert the moment the real route lands.
     *
     * The controller and this test were written in one unit; the route in
     * routes/web.php is a separate edit to a shared file. Until that edit
     * lands every case here would 404 and prove nothing about the SQL it is
     * actually here to pin, so the route is declared locally — but ONLY when
     * the application has not declared it. Once routes/web.php names
     * `foundry.attribute_tables` this method returns immediately and the
     * suite exercises the real wiring, middleware and all.
     *
     * {@see test_the_page_is_wired_into_routes_web_php()} is what stops this
     * shim from quietly standing in for the wiring forever: it fails for as
     * long as the real route is missing.
     */
    private function registerRouteUntilWired(): void
    {
        if (Route::has('foundry.attribute_tables')) {
            return;
        }

        Route::middleware(['web', 'auth:sanctum'])
            ->get('/projects/{slug}/attribute-tables', [AttributeTablesController::class, 'index'])
            ->where('slug', '[a-z0-9\-]+')
            ->name('foundry.attribute_tables');
    }

    public function test_the_page_is_wired_into_routes_web_php(): void
    {
        // Deliberately reads the file rather than Route::has(): setUp's shim
        // above satisfies Route::has() by itself, so asking the router would
        // let the shim answer for the wiring it is standing in for.
        $web = (string) file_get_contents(base_path('routes/web.php'));

        $this->assertStringContainsString(
            'AttributeTablesController',
            $web,
            'routes/web.php does not route anything to AttributeTablesController, so '
            .'/projects/{slug}/attribute-tables 404s in the real application and '
            .'silver.attribute_tables still has no reader. Add:'."\n\n"
            .'    Route::get(\'/projects/{slug}/attribute-tables\', '
            .'[AttributeTablesController::class, \'index\'])'."\n"
            .'        ->where(\'slug\', \'[a-z0-9\-]+\')'."\n"
            .'        ->name(\'foundry.attribute_tables\');',
        );
    }

    /**
     * @param array<int, array<string, mixed>> $rows
     */
    private function insertTable(string $sha, string $layer, string $file, array $rows): void
    {
        foreach ($rows as $index => $attributes) {
            DB::table('silver.attribute_tables')->insert([
                'workspace_id' => $this->workspaceId,
                'project_id' => $this->project->project_id,
                'source_file' => $file,
                'source_file_sha256' => $sha,
                'source_layer' => $layer,
                'row_index' => $index,
                'attributes' => json_encode($attributes),
            ]);
        }
    }

    /**
     * A soil survey shaped like the real one: every value a STRING, because
     * the delimited fallback reads rows with csv.DictReader and csv gives
     * strings. `au_ppm` is numeric-looking with one blank cell — the case
     * that decides whether a whole assay column right-aligns.
     */
    private function seedSoils(int $rows = 3): void
    {
        $seed = [];
        for ($i = 0; $i < $rows; $i++) {
            $seed[] = [
                'sample_id' => 'RS-'.(1000 + $i),
                'au_ppm' => $i === 1 ? '' : (string) (0.001 * $i),
                'grainsize' => 'fine',
            ];
        }
        $this->insertTable(self::SOILS_SHA, 'all_historical_soils_clean', 'all_historical_soils_clean.DAT', $seed);
    }

    // ── Access ──────────────────────────────────────────────────────────

    public function test_a_non_member_gets_404_not_403(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get("/projects/{$this->project->slug}/attribute-tables")
            ->assertStatus(404);
    }

    // ── The index: what tables do I have? ───────────────────────────────

    public function test_the_index_lists_one_entry_per_file_and_layer_with_a_row_count(): void
    {
        $this->seedSoils(3);
        $this->insertTable(self::LEGEND_SHA, 'legend', 'legend.dbf', [
            ['code' => 'GR', 'label' => 'Granite'],
            ['code' => 'SS', 'label' => 'Sandstone'],
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/attribute-tables")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/AttributeTables')
                    ->where('project.slug', $this->project->slug)
                    ->has('tables', 2)
                    // Ordered by layer: all_historical_soils_clean, legend.
                    ->where('tables.0.source_layer', 'all_historical_soils_clean')
                    ->where('tables.0.source_file', 'all_historical_soils_clean.DAT')
                    ->where('tables.0.rows', 3)
                    ->where('tables.1.source_layer', 'legend')
                    ->where('tables.1.rows', 2)
                    ->where('selected', null)
                    ->where('table', null),
            );
    }

    public function test_another_projects_tables_are_not_listed(): void
    {
        $this->seedSoils(3);

        $other = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $other->project_id],
        );
        DB::table('silver.attribute_tables')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $other->project_id,
            'source_file' => 'someone_elses.dbf',
            'source_file_sha256' => self::LEGEND_SHA,
            'source_layer' => 'someone_elses',
            'row_index' => 0,
            'attributes' => json_encode(['x' => 1]),
        ]);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/attribute-tables")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->has('tables', 1)
                    ->where('tables.0.source_layer', 'all_historical_soils_clean'),
            );
    }

    // ── RLS ─────────────────────────────────────────────────────────────

    /**
     * The regression this whole file exists for.
     *
     * silver.attribute_tables carries FORCE ROW LEVEL SECURITY and a
     * fail-closed policy, so a query that does not bind `app.workspace_id`
     * matches ZERO rows — for the table owner too. Dropping
     * withWorkspaceRls() from the controller therefore does not raise: it
     * renders a page that looks perfectly healthy and is empty.
     *
     * This case proves the controller's own path is inside the context, by
     * showing that the SAME query outside it returns nothing while the page
     * returns the rows. Without the second half the assertion would pass on
     * a cluster where RLS is not enforced at all and prove nothing, so both
     * halves are required.
     */
    public function test_the_page_returns_rows_that_a_query_without_the_rls_context_cannot_see(): void
    {
        $this->seedSoils(3);

        // Only meaningful as georag_app: the suite connects as georag, which
        // owns the table, and locally that role is a superuser with
        // BYPASSRLS — policies do not apply to it at all.
        $enforced = DB::selectOne(<<<'SQL'
            SELECT EXISTS (
                SELECT 1 FROM pg_roles
                 WHERE rolname = 'georag_app' AND rolbypassrls = false
            ) AS present
        SQL);

        if ($enforced->present ?? false) {
            $blind = DB::transaction(function (): int {
                DB::statement('SET LOCAL ROLE georag_app');

                return (int) DB::table('silver.attribute_tables')
                    ->where('project_id', $this->project->project_id)
                    ->count();
            });

            $this->assertSame(
                0,
                $blind,
                'silver.attribute_tables answered a query with no app.workspace_id '
                .'bound. The fail-closed policy or FORCE ROW LEVEL SECURITY has '
                .'been weakened, and the assertion below no longer proves the '
                .'controller sets the RLS context.',
            );
        }

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/attribute-tables")
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->has('tables', 1)
                    ->where('tables.0.rows', 3),
            );
    }

    // ── Opening one table ───────────────────────────────────────────────

    public function test_opening_a_table_returns_derived_columns_and_rows(): void
    {
        $this->seedSoils(3);

        $this->actingAs($this->user)
            ->get($this->tableUrl(self::SOILS_SHA, 'all_historical_soils_clean'))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->component('Foundry/AttributeTables')
                    ->where('selected.source_file_sha256', self::SOILS_SHA)
                    ->where('selected.source_layer', 'all_historical_soils_clean')
                    ->where('table.total_rows', 3)
                    ->where('table.source_file', 'all_historical_soils_clean.DAT')
                    // Alphabetical, C collation — jsonb has already discarded
                    // the source file's own column order by storage time.
                    ->where('table.columns.0.name', 'au_ppm')
                    ->where('table.columns.1.name', 'grainsize')
                    ->where('table.columns.2.name', 'sample_id')
                    ->has('table.rows', 3)
                    ->where('table.rows.0.row_index', 0)
                    // Cells are positional, aligned with columns.
                    ->where('table.rows.0.cells.2', 'RS-1000'),
            );
    }

    /**
     * The delimited-fallback writer hands every value to json.dumps as a
     * STRING, so an assay column arrives as `"0.002"`, not `0.002`. Deciding
     * numeric-ness on jsonb_typeof alone would left-align every column of
     * every CSV-sourced table.
     *
     * The blank cell in row 1 is the other half: a CSV blank is `''`, and
     * counting it as text would drag the whole column back to left-aligned.
     */
    public function test_a_numeric_looking_string_column_with_a_blank_cell_is_still_numeric(): void
    {
        $this->seedSoils(3);

        $this->actingAs($this->user)
            ->get($this->tableUrl(self::SOILS_SHA, 'all_historical_soils_clean'))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('table.columns.0.name', 'au_ppm')
                    ->where('table.columns.0.numeric', true)
                    ->where('table.columns.1.name', 'grainsize')
                    ->where('table.columns.1.numeric', false)
                    ->where('table.columns.2.name', 'sample_id')
                    // 'RS-1000' is not a number, however much it looks like
                    // an identifier a spreadsheet would right-align.
                    ->where('table.columns.2.numeric', false),
            );
    }

    public function test_a_column_of_nothing_but_blanks_is_not_numeric(): void
    {
        $this->insertTable(self::LEGEND_SHA, 'legend', 'legend.dbf', [
            ['code' => 'GR', 'note' => ''],
            ['code' => 'SS', 'note' => null],
        ]);

        $this->actingAs($this->user)
            ->get($this->tableUrl(self::LEGEND_SHA, 'legend'))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('table.columns.1.name', 'note')
                    // Vacuously "every filled value is a number" — there are
                    // none. Right-aligning an empty column is noise.
                    ->where('table.columns.1.numeric', false),
            );
    }

    /**
     * The header is derived from a BOUNDED sample, so it can miss a key that
     * first appears deeper in the file. The payload has to say so rather
     * than drop the column silently — that is the failure mode the bound
     * buys, and hiding it makes the page lie about the data.
     */
    public function test_a_key_the_header_sample_missed_is_reported_not_dropped(): void
    {
        // 51 rows: the sample reads 50, and only row 50 carries `late_key`.
        $rows = [];
        for ($i = 0; $i < 51; $i++) {
            $rows[] = $i === 50
                ? ['code' => 'X'.$i, 'late_key' => 'appeared late']
                : ['code' => 'X'.$i];
        }
        $this->insertTable(self::LEGEND_SHA, 'legend', 'legend.dbf', $rows);

        $this->actingAs($this->user)
            ->get($this->tableUrl(self::LEGEND_SHA, 'legend', 2))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('table.sampled_rows', 50)
                    ->where('table.total_rows', 51)
                    ->has('table.columns', 1)
                    ->where('table.columns.0.name', 'code')
                    ->where('table.extra_columns_on_page', ['late_key']),
            );
    }

    public function test_paging_walks_the_rows_in_row_index_order(): void
    {
        $rows = [];
        for ($i = 0; $i < 120; $i++) {
            $rows[] = ['code' => 'X'.$i];
        }
        $this->insertTable(self::LEGEND_SHA, 'legend', 'legend.dbf', $rows);

        $this->actingAs($this->user)
            ->get($this->tableUrl(self::LEGEND_SHA, 'legend', 2))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('table.page', 2)
                    ->where('table.last_page', 3)
                    ->has('table.rows', 50)
                    ->where('table.rows.0.row_index', 50)
                    ->where('table.rows.0.cells.0', 'X50')
                    ->where('table.rows.49.row_index', 99),
            );

        // Past the end clamps to the last page rather than rendering an
        // empty grid — a stale bookmark should show data, not a blank.
        $this->actingAs($this->user)
            ->get($this->tableUrl(self::LEGEND_SHA, 'legend', 40))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('table.page', 3)
                    ->has('table.rows', 20),
            );
    }

    /**
     * The CHECK constraint pins the TOP level to an object; the values are
     * unconstrained. A nested array reaching React as a raw value renders as
     * "[object Object]" at best, so the controller flattens it to its JSON
     * text.
     */
    public function test_a_nested_value_is_flattened_to_json_text(): void
    {
        $this->insertTable(self::LEGEND_SHA, 'legend', 'legend.dbf', [
            ['code' => 'GR', 'tags' => ['felsic', 'intrusive']],
        ]);

        $this->actingAs($this->user)
            ->get($this->tableUrl(self::LEGEND_SHA, 'legend'))
            ->assertOk()
            ->assertInertia(
                fn (AssertableInertia $page) => $page
                    ->where('table.columns.1.name', 'tags')
                    ->where('table.rows.0.cells.1', '["felsic","intrusive"]'),
            );
    }

    // ── Refusals ────────────────────────────────────────────────────────

    public function test_a_table_from_another_project_404s_rather_than_opening(): void
    {
        $other = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $other->project_id],
        );
        DB::table('silver.attribute_tables')->insert([
            'workspace_id' => $this->workspaceId,
            'project_id' => $other->project_id,
            'source_file' => 'someone_elses.dbf',
            'source_file_sha256' => self::LEGEND_SHA,
            'source_layer' => 'someone_elses',
            'row_index' => 0,
            'attributes' => json_encode(['x' => 1]),
        ]);

        // The sha + layer are real; they just are not this project's. 404,
        // not an empty table — an empty table reads as "the file landed with
        // no rows", which is a different and much more alarming story.
        $this->actingAs($this->user)
            ->get($this->tableUrl(self::LEGEND_SHA, 'someone_elses'))
            ->assertStatus(404);
    }

    public function test_a_malformed_sha_404s(): void
    {
        $this->seedSoils(1);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/attribute-tables?table=nope&layer=legend")
            ->assertStatus(404);
    }

    public function test_a_sha_without_a_layer_404s(): void
    {
        // Both halves are the identity: the layer name for a bare .dbf is
        // the file stem, and one hash can carry several layers when the
        // source is a workbook.
        $this->seedSoils(1);

        $this->actingAs($this->user)
            ->get("/projects/{$this->project->slug}/attribute-tables?table=".self::SOILS_SHA)
            ->assertStatus(404);
    }

    private function tableUrl(string $sha, string $layer, ?int $page = null): string
    {
        $query = ['table' => $sha, 'layer' => $layer];
        if ($page !== null) {
            $query['page'] = (string) $page;
        }

        return "/projects/{$this->project->slug}/attribute-tables?".http_build_query($query);
    }
}
