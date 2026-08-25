<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/AttributeTablesController — the read path for
 * `silver.attribute_tables`.
 *
 *   GET /projects/{slug}/attribute-tables
 *   GET /projects/{slug}/attribute-tables?table={sha256}&layer={name}&page=2
 *
 * WHY THIS EXISTS
 *   Until this controller, `silver.attribute_tables` had ZERO readers — no
 *   route, no page, no MVT function, no agent tool. Standalone `.dbf`
 *   ingestion landed 2026-08-23 and MapInfo `.dat` the day after, and the
 *   delimited fallback in `ingest_tabular._land_unclassified_as_rows()`
 *   writes there too, so a single real delivery now puts hundreds of rows
 *   into a table nothing could show: measured at 229 rows from one RedStar
 *   project and 854 from one soil survey. Every format added to the writer
 *   made the gap wider.
 *
 * WHY THE COLUMNS ARE COMPUTED
 *   `attributes` is free-form jsonb — a dBASE table's columns are whatever
 *   the person who made it typed — so there is no fixed header to render.
 *   Deriving it by scanning every row would make the page get slower as
 *   data lands, on a table whose whole purpose is accumulating rows nothing
 *   else has a schema for. The header therefore comes from a BOUNDED head
 *   sample (see {@see COLUMN_SAMPLE_ROWS}) and the payload says so, because
 *   a sample can miss a key and a silently-missing column is worse than a
 *   visible caveat.
 *
 * RLS
 *   `silver.attribute_tables` carries FORCE ROW LEVEL SECURITY and a
 *   fail-closed policy (`workspace_id = NULLIF(current_setting(
 *   'app.workspace_id', true), '')::uuid`). Unbound GUC means the
 *   comparison is NULL, which means ZERO rows — for the table owner too,
 *   because of FORCE. Every query below therefore runs inside
 *   {@see SetsWorkspaceRlsContext::withWorkspaceRls()}. Dropping that
 *   wrapper does not raise: it renders an empty page that looks fine, which
 *   is the single most likely way to ship this broken.
 *   AttributeTablesControllerTest's
 *   `test_the_page_returns_rows_that_a_query_without_the_rls_context_cannot_see()`
 *   pins that behaviour so the wrapper cannot be removed quietly.
 *
 * Props are closures for the same reason ReportController's are: selecting a
 * table in the UI is an Inertia partial reload asking only for the detail
 * props, and a closure prop that is not requested is never evaluated.
 */
class AttributeTablesController extends Controller
{
    use SetsWorkspaceRlsContext;

    /**
     * How many (file, layer) pairs the master list shows.
     *
     * A cap rather than true pagination: a project with more than 200
     * distinct attribute tables has an ingestion problem to look at first,
     * and an unbounded GROUP BY here is what would make the list itself the
     * slow part of the page.
     */
    private const TABLE_LIST_LIMIT = 200;

    /**
     * Rows read to derive the column header.
     *
     * The trade this constant makes: a key that appears for the first time
     * after row 50 gets no column, so its values would be invisible. That is
     * why {@see rowPage()} also reports `extra_columns_on_page` — keys the
     * sample missed but the rendered page actually contains — instead of
     * dropping them without a word.
     */
    private const COLUMN_SAMPLE_ROWS = 50;

    /** Rows per page in the detail pane. */
    private const ROWS_PER_PAGE = 50;

    /**
     * Longest cell rendered. A `.dbf` memo field can hold a whole comment
     * log; a table cell holding 4 KB of prose destroys the row grid and
     * ships megabytes of props for a page nobody can read.
     */
    private const CELL_MAX_CHARS = 300;

    public function index(Request $request, string $slug): Response
    {
        $project = $this->resolveProject($request, $slug);

        $selected = $this->selectedTable($request, $project);

        return Inertia::render('Foundry/AttributeTables', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'tables' => fn () => $this->tableList($project),
            'selected' => $selected,
            'table' => $selected === null
                ? null
                : fn () => $this->tableDetail(
                    $project,
                    $selected['source_file_sha256'],
                    $selected['source_layer'],
                    $this->requestedPage($request),
                ),
        ]);
    }

    /**
     * Resolve the project and assert the caller is a member of it.
     *
     * Identical to ReportController::resolveProject — a non-member gets the
     * 404 that firstOrFail() raises, never a 403, so probing cannot tell
     * "not yours" from "does not exist".
     */
    private function resolveProject(Request $request, string $slug): Project
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        return $project;
    }

    /**
     * The (sha256, layer) pair named by the query string, or null.
     *
     * Both halves are required: `source_layer` alone is not unique — the
     * layer name for a bare `.dbf` is the file stem, and two different
     * exports called `soils.dbf` are two different tables that hash
     * differently. The UNIQUE constraint on the table is keyed the same way.
     *
     * A malformed or unknown pair 404s rather than silently falling back to
     * "nothing selected", which would look identical to a table that exists
     * but is empty.
     *
     * @return array{source_file_sha256: string, source_layer: string}|null
     */
    private function selectedTable(Request $request, Project $project): ?array
    {
        $sha = $request->query('table');
        $layer = $request->query('layer');

        if (! is_string($sha) || $sha === '') {
            return null;
        }

        if (! preg_match('/^[0-9a-f]{64}$/', $sha) || ! is_string($layer) || $layer === '') {
            abort(404, 'Attribute table not found in this project.');
        }

        $exists = $this->withWorkspaceRls(
            (string) $project->workspace_id,
            fn (): bool => DB::table('silver.attribute_tables')
                ->where('project_id', $project->project_id)
                ->where('source_file_sha256', $sha)
                ->where('source_layer', $layer)
                ->exists(),
        );

        if (! $exists) {
            abort(404, 'Attribute table not found in this project.');
        }

        return ['source_file_sha256' => $sha, 'source_layer' => $layer];
    }

    /** 1-based page number from the query string, floor-clamped at 1. */
    private function requestedPage(Request $request): int
    {
        $page = $request->query('page');

        return is_numeric($page) ? max(1, (int) $page) : 1;
    }

    /**
     * The master list: one entry per distinct table in this project.
     *
     * Grouped on (source_file_sha256, source_layer) rather than on the
     * filename, because that pair IS the identity — it is what the table's
     * UNIQUE constraint is keyed on and what the detail pane selects by. The
     * filename comes along via MAX() as the label: the upsert rewrites
     * `source_file` on every re-ingest so all rows of one group already
     * agree on it, and MAX() keeps the aggregate valid if a historical row
     * ever disagrees.
     *
     * @return array<int, array{source_file: string|null, source_layer: string, source_file_sha256: string, rows: int, updated_at: string|null}>
     */
    private function tableList(Project $project): array
    {
        $rows = $this->withWorkspaceRls(
            (string) $project->workspace_id,
            fn () => DB::table('silver.attribute_tables')
                ->where('project_id', $project->project_id)
                ->groupBy('source_file_sha256', 'source_layer')
                ->orderBy('source_layer')
                ->limit(self::TABLE_LIST_LIMIT)
                ->select(
                    'source_file_sha256',
                    'source_layer',
                    DB::raw('MAX(source_file) AS source_file'),
                    DB::raw('COUNT(*) AS row_count'),
                    DB::raw('MAX(updated_at) AS updated_at'),
                )
                ->get(),
        );

        return $rows->map(fn ($r) => $this->tableListRow($r))->values()->all();
    }

    /**
     * One entry in the master list.
     *
     * A named method with a declared return type, for the reason
     * ReportController::reportListRow() documents: PHPStan infers the exact
     * array shape from an inline literal and that shape is not a subtype of
     * the loose `array<string, mixed>` the caller promises, so every field
     * added here would otherwise break the caller's return type.
     *
     * @return array{source_file: string|null, source_layer: string, source_file_sha256: string, rows: int, updated_at: string|null}
     */
    private function tableListRow(object $r): array
    {
        $file = $r->source_file ?? null;

        return [
            'source_file' => is_string($file) && $file !== '' ? $file : null,
            'source_layer' => (string) $r->source_layer,
            'source_file_sha256' => (string) $r->source_file_sha256,
            'rows' => (int) $r->row_count,
            'updated_at' => isset($r->updated_at) ? (string) $r->updated_at : null,
        ];
    }

    /**
     * One table: a derived header plus a page of rows.
     *
     * Both halves run in ONE withWorkspaceRls transaction. Not for speed —
     * for agreement. Two transactions could straddle a concurrent re-ingest
     * and pair a header derived from the old rows with a page of the new
     * ones, so a renamed column would render every cell under it blank.
     *
     * @return array<string, mixed>
     */
    private function tableDetail(Project $project, string $sha, string $layer, int $page): array
    {
        return $this->withWorkspaceRls(
            (string) $project->workspace_id,
            function () use ($project, $sha, $layer, $page): array {
                $columns = $this->deriveColumns($project, $sha, $layer);

                // Count and label in one pass — the UNIQUE index on
                // (project_id, source_file_sha256, source_layer, row_index)
                // makes this table's rows contiguous, so it is an index
                // range scan rather than a heap sweep.
                $meta = DB::table('silver.attribute_tables')
                    ->where('project_id', $project->project_id)
                    ->where('source_file_sha256', $sha)
                    ->where('source_layer', $layer)
                    ->selectRaw('COUNT(*) AS row_count, MAX(source_file) AS source_file')
                    ->first();

                $total = (int) ($meta->row_count ?? 0);
                $sourceFile = $meta->source_file ?? null;

                // Clamp past-the-end rather than render an empty grid: a
                // stale bookmark to ?page=40 on a table that shrank to 200
                // rows should show the last page, not nothing.
                $lastPage = max(1, (int) ceil($total / self::ROWS_PER_PAGE));
                $page = min($page, $lastPage);

                return array_merge(
                    [
                        'source_file' => is_string($sourceFile) && $sourceFile !== ''
                            ? $sourceFile
                            : null,
                        'source_file_sha256' => $sha,
                        'source_layer' => $layer,
                        'columns' => $columns,
                        'sampled_rows' => min($total, self::COLUMN_SAMPLE_ROWS),
                        'total_rows' => $total,
                        'page' => $page,
                        'per_page' => self::ROWS_PER_PAGE,
                        'last_page' => $lastPage,
                    ],
                    $this->rowPage($project, $sha, $layer, $columns, $page),
                );
            },
        );
    }

    /**
     * Derive the column header from a bounded head sample of the rows.
     *
     * COLUMN ORDER is alphabetical (C collation), NOT the source file's own
     * order. That order is not recoverable: jsonb normalises object keys on
     * write — sorted by length then bytes — so by the time a row is stored,
     * the sequence the geologist's export had is gone. Alphabetical is at
     * least stable page to page and is the order that makes one column
     * findable among the 111 a real soil survey carries.
     *
     * `numeric` drives right-alignment and tabular-nums in the UI, and it is
     * computed here rather than guessed in the browser because only the
     * server sees enough rows to decide. It counts a numeric-looking STRING
     * as numeric on purpose: the two writers disagree about types.
     * `_write_attribute_rows()` reached from the dBASE branch gets real
     * floats out of GDAL, while the delimited fallback
     * (`_land_unclassified_as_rows` → `_read_delimited_rows`) uses
     * `csv.DictReader`, whose every value is a string. A column of assay
     * values must right-align in both.
     *
     * Empty strings count as absent, not as text — a CSV blank cell in an
     * otherwise numeric column must not left-align the whole column.
     *
     * Only dot-decimals are recognised. A decimal-comma export ("1,589")
     * reads as text here, which is the honest outcome: it would render as
     * text too.
     *
     * @return array<int, array{name: string, numeric: bool}>
     */
    private function deriveColumns(Project $project, string $sha, string $layer): array
    {
        // The sample size is interpolated from an int class constant rather
        // than bound: PDO sends bound parameters as strings, and Postgres
        // rejects `LIMIT '50'` outright.
        $sql = sprintf(<<<'SQL'
            WITH sample AS (
                SELECT attributes
                  FROM silver.attribute_tables
                 WHERE project_id = ?::uuid
                   AND source_file_sha256 = ?
                   AND source_layer = ?
                 ORDER BY row_index
                 LIMIT %d
            )
            SELECT k.key AS name,
                   COUNT(*) FILTER (
                       WHERE jsonb_typeof(s.attributes -> k.key) <> 'null'
                         AND s.attributes ->> k.key <> ''
                   ) AS filled,
                   COUNT(*) FILTER (
                       WHERE jsonb_typeof(s.attributes -> k.key) = 'number'
                          OR (jsonb_typeof(s.attributes -> k.key) = 'string'
                              AND s.attributes ->> k.key ~ '^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$')
                   ) AS numeric_like
              FROM sample s
              CROSS JOIN LATERAL jsonb_object_keys(s.attributes) AS k(key)
             GROUP BY k.key
             ORDER BY k.key COLLATE "C"
            SQL, self::COLUMN_SAMPLE_ROWS);

        $rows = DB::select($sql, [$project->project_id, $sha, $layer]);

        return array_map(fn ($r) => $this->columnSpec($r), $rows);
    }

    /**
     * One derived column. Named for the PHPStan array-shape reason above.
     *
     * @return array{name: string, numeric: bool}
     */
    private function columnSpec(object $r): array
    {
        $filled = (int) $r->filled;

        return [
            'name' => (string) $r->name,
            // A column of nothing but blanks is not numeric. Requiring
            // filled > 0 keeps an all-empty column left-aligned instead of
            // trivially satisfying "every filled value is a number".
            'numeric' => $filled > 0 && (int) $r->numeric_like === $filled,
        ];
    }

    /**
     * One page of rows, flattened to cell arrays aligned with $columns.
     *
     * Arrays rather than per-row objects: at 111 columns × 50 rows, repeating
     * every key in every row roughly triples the prop payload for data the
     * client already has in the header.
     *
     * @param array<int, array{name: string, numeric: bool}> $columns
     *
     * @return array{rows: array<int, array{row_index: int, cells: array<int, string|int|float|bool|null>}>, extra_columns_on_page: array<int, string>}
     */
    private function rowPage(Project $project, string $sha, string $layer, array $columns, int $page): array
    {
        $names = array_column($columns, 'name');

        $raw = DB::table('silver.attribute_tables')
            ->where('project_id', $project->project_id)
            ->where('source_file_sha256', $sha)
            ->where('source_layer', $layer)
            ->orderBy('row_index')
            ->offset(($page - 1) * self::ROWS_PER_PAGE)
            ->limit(self::ROWS_PER_PAGE)
            ->select('row_index', 'attributes')
            ->get();

        $known = array_flip($names);
        $extra = [];
        $rows = [];

        foreach ($raw as $r) {
            $attributes = is_string($r->attributes)
                ? json_decode($r->attributes, true)
                : $r->attributes;
            if (! is_array($attributes)) {
                // chk_attribute_tables_attributes_object makes a non-object
                // unstorable, so this is unreachable through the writer —
                // treated as an empty row rather than trusted, because the
                // alternative is a TypeError that takes the page down.
                $attributes = [];
            }

            $cells = [];
            foreach ($names as $name) {
                $cells[] = $this->normaliseCell($attributes[$name] ?? null);
            }

            // Keys this page has that the header sample never saw. Reported
            // rather than dropped: silently omitting a column is exactly the
            // failure the bounded sample risks, and a user who can see the
            // caveat can go look at the source file.
            foreach (array_keys($attributes) as $key) {
                if (! isset($known[$key])) {
                    $extra[(string) $key] = true;
                }
            }

            $rows[] = ['row_index' => (int) $r->row_index, 'cells' => $cells];
        }

        return ['rows' => $rows, 'extra_columns_on_page' => array_keys($extra)];
    }

    /**
     * Flatten one jsonb value to something a table cell can render.
     *
     * The CHECK constraint pins the TOP level to an object; the values
     * inside it are unconstrained, and a nested array or object reaching
     * React as a raw value renders as "[object Object]" at best and throws
     * at worst. Nested values become their JSON text so the cell shows what
     * is actually stored.
     *
     * @param mixed $value
     */
    private function normaliseCell($value): string|int|float|bool|null
    {
        if ($value === null || is_int($value) || is_float($value) || is_bool($value)) {
            return $value;
        }

        if (! is_string($value)) {
            $value = json_encode($value, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
            if (! is_string($value)) {
                return null;
            }
        }

        return mb_strlen($value) > self::CELL_MAX_CHARS
            ? mb_substr($value, 0, self::CELL_MAX_CHARS).'…'
            : $value;
    }
}
