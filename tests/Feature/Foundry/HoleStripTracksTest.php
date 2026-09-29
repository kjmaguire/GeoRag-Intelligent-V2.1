<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use App\Support\HoleStripTracks;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * The strip log is given lithology it can colour, alteration and mineralization.
 *
 * `gold.drillhole_intervals_visual` was read for `lithology` bands only, and
 * `color_hint` carried whatever text the promotion wrote (a described colour, a
 * rock code) straight into a CSS fill. Alteration and mineralization - written
 * by ingest_tabular to silver.alteration / silver.mineralization, promoted to
 * gold as the `alteration` and `mineralization` kinds (§04e, SME-approved
 * 2026-09-29) - were drawn nowhere.
 *
 * Covered: the payload shape, the display-colour rule, the attributes gold has
 * no column for, the bound, that a hole with a logged strip but no curves is
 * reachable from the LOGS picker at all, and that mineralization is read from
 * gold (one row per interval, flattened to one band per mineral) - a
 * silver-only row is not drawn.
 *
 * Postgres-only (phpunit.pgsql.xml): PostGIS collars, JSONB payloads.
 */
final class HoleStripTracksTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private string $workspaceId;

    /**
     * @return array{user: User, project: Project}
     */
    private function seedProject(): array
    {
        $user = User::factory()->create();

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Strip Tracks Workspace', 'hst-'.substr($this->workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        return ['user' => $user, 'project' => $project];
    }

    private function seedCollar(Project $project, string $holeId): string
    {
        $collarId = (string) Str::uuid();
        DB::statement(
            "INSERT INTO silver.collars (
                collar_id, hole_id, project_id, workspace_id,
                easting, northing, elevation, total_depth, azimuth, dip,
                hole_type, status, geom_4326
             ) VALUES (
                ?::uuid, ?, ?::uuid, ?::uuid,
                500000, 4500000, 1000, 150, 180, -60,
                'Diamond', 'Completed',
                ST_Transform(ST_SetSRID(ST_MakePoint(500000, 4500000), 32613), 4326)
             )",
            [$collarId, $holeId, $project->project_id, $this->workspaceId],
        );

        return $collarId;
    }

    private function seedLithology(
        Project $project,
        string $collarId,
        float $from,
        float $to,
        string $code,
        ?string $label,
        ?string $colorHint,
    ): void {
        DB::statement(
            "INSERT INTO gold.drillhole_intervals_visual (
                collar_id, workspace_id, project_id, depth_from, depth_to,
                interval_kind, lithology_code, lithology_label, color_hint
             ) VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, 'lithology', ?, ?, ?)",
            [$collarId, $this->workspaceId, $project->project_id, $from, $to, $code, $label, $colorHint],
        );
    }

    private function seedLithologyLog(
        string $collarId,
        float $from,
        float $to,
        string $code,
        array $attrs = [],
    ): void {
        DB::statement(
            'INSERT INTO silver.lithology_logs (
                log_id, workspace_id, collar_id, from_depth, to_depth,
                lithology_code, lithology_description, grain_size, color,
                hardness, rqd, recovery, weathering, created_at, updated_at
             ) VALUES (gen_random_uuid(), ?::uuid, ?::uuid, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())',
            [
                $this->workspaceId, $collarId, $from, $to, $code,
                $attrs['description'] ?? null, $attrs['grain_size'] ?? null,
                $attrs['color'] ?? null, $attrs['hardness'] ?? null,
                $attrs['rqd'] ?? null, $attrs['recovery'] ?? null,
                $attrs['weathering'] ?? null,
            ],
        );
    }

    private function seedAlteration(Project $project, string $collarId, float $from, float $to, string $payload, string $label): void
    {
        DB::statement(
            "INSERT INTO gold.drillhole_intervals_visual (
                collar_id, workspace_id, project_id, depth_from, depth_to,
                interval_kind, lithology_label, alteration_payload
             ) VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, 'alteration', ?, ?::jsonb)",
            [$collarId, $this->workspaceId, $project->project_id, $from, $to, $label, $payload],
        );
    }

    /**
     * A gold `mineralization` row: one interval, every mineral of it in the
     * payload (what promote_silver_to_gold writes).
     *
     * @param list<array{mineral: string, abundance_pct?: ?float, form?: ?string, grain_size?: ?string, notes?: ?string}> $minerals
     */
    private function seedMineralization(Project $project, string $collarId, float $from, float $to, array $minerals): void
    {
        $payload = ['minerals' => array_map(fn (array $m) => [
            'mineral' => $m['mineral'],
            'abundance_pct' => $m['abundance_pct'] ?? null,
            'form' => $m['form'] ?? null,
            'grain_size' => $m['grain_size'] ?? null,
            'notes' => $m['notes'] ?? null,
        ], $minerals)];

        DB::statement(
            "INSERT INTO gold.drillhole_intervals_visual (
                collar_id, workspace_id, project_id, depth_from, depth_to,
                interval_kind, lithology_label, mineralization_payload
             ) VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, 'mineralization', ?, ?::jsonb)",
            [
                $collarId, $this->workspaceId, $project->project_id, $from, $to,
                implode('; ', array_column($minerals, 'mineral')), json_encode($payload),
            ],
        );
    }

    /** The raw record: one silver.mineralization row per mineral (the collar API reads this). */
    private function seedSilverMineralization(string $collarId, float $from, float $to, string $mineral, ?float $pct): void
    {
        DB::statement(
            'INSERT INTO silver.mineralization (
                id, workspace_id, collar_id, from_depth, to_depth, mineral,
                abundance_pct, form, grain_size, notes
             ) VALUES (gen_random_uuid(), ?::uuid, ?::uuid, ?, ?, ?, ?, ?, NULL, NULL)',
            [$this->workspaceId, $collarId, $from, $to, $mineral, $pct, 'Disseminated'],
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function workspaceProps(User $user, Project $project, string $query = ''): array
    {
        $props = [];
        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/workspace'.$query)
            ->assertStatus(200)
            ->assertInertia(function (AssertableInertia $page) use (&$props) {
                $props = $page->toArray()['props'];

                return $page;
            });

        return $props;
    }

    public function test_lithology_carries_a_display_colour_or_nothing(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedLithology($project, $collar, 0, 5, 'GRN', 'Grey granite', '#8899AA');
        // The old promotion wrote the colour TEXT and the rock CODE here.
        $this->seedLithology($project, $collar, 5, 10, 'SST', 'Sandstone', 'dark grey');
        $this->seedLithology($project, $collar, 10, 15, 'MDS', null, 'MDS');
        $this->seedLithology($project, $collar, 15, 20, 'SHL', 'Shale', null);

        $bands = (new HoleStripTracks)->lithology($collar)['bands'];

        $this->assertSame(['#8899aa', '', '', ''], array_column($bands, 'color'));
        $this->assertSame(['GRN', 'SST', 'MDS', 'SHL'], array_column($bands, 'code'));
        $this->assertSame([0.0, 5.0, 10.0, 15.0], array_column($bands, 'from'));
    }

    public function test_lithology_gets_the_attributes_gold_has_no_column_for(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedLithology($project, $collar, 0, 5, 'GRN', 'Grey granite', null);
        $this->seedLithologyLog($collar, 0, 5, 'GRN', [
            'description' => 'Grey granite', 'grain_size' => 'Fine', 'color' => 'dark grey',
            'hardness' => 'Hard', 'rqd' => 85, 'recovery' => 98, 'weathering' => 'Fresh',
        ]);
        $this->seedLithology($project, $collar, 5, 10, 'SST', 'Sandstone', null); // no log row

        $bands = (new HoleStripTracks)->lithology($collar)['bands'];

        $this->assertSame([
            'description' => 'Grey granite', 'colour' => 'dark grey', 'grain_size' => 'Fine',
            'hardness' => 'Hard', 'weathering' => 'Fresh', 'rqd' => 85.0, 'recovery' => 98.0,
        ], $bands[0]['detail']);
        $this->assertArrayNotHasKey('detail', $bands[1]);
    }

    public function test_alteration_bands_carry_every_alteration_of_the_interval(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedAlteration(
            $project, $collar, 0, 5,
            json_encode(['alterations' => [
                ['type' => 'Chlorite', 'intensity' => 'Strong', 'minerals' => ['chlorite', 'sericite'], 'notes' => null],
                ['type' => 'Silica', 'intensity' => null, 'minerals' => [], 'notes' => 'patchy'],
            ]]),
            'Chlorite (Strong); Silica',
        );

        $bands = (new HoleStripTracks)->alteration($collar)['bands'];

        $this->assertCount(1, $bands);
        $this->assertSame('Chlorite (Strong); Silica', $bands[0]['label']);
        $this->assertSame([
            ['type' => 'Chlorite', 'intensity' => 'Strong', 'minerals' => ['chlorite', 'sericite'], 'notes' => null],
            ['type' => 'Silica', 'intensity' => null, 'minerals' => [], 'notes' => 'patchy'],
        ], $bands[0]['alterations']);
    }

    public function test_a_malformed_alteration_payload_yields_no_alterations_not_an_error(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedAlteration($project, $collar, 0, 5, '{}', 'x');

        $bands = (new HoleStripTracks)->alteration($collar)['bands'];

        $this->assertSame([], $bands[0]['alterations']);
    }

    public function test_mineralization_is_read_from_gold_flattened_to_one_band_per_mineral(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        // Payload order is the order the bands come back in (silver created_at, id).
        $this->seedMineralization($project, $collar, 5, 10, [
            ['mineral' => 'Pyrite', 'abundance_pct' => 3.0, 'form' => 'Disseminated', 'grain_size' => 'Fine', 'notes' => 'vein-hosted'],
            ['mineral' => 'Chalcopyrite'],
        ]);
        $this->seedMineralization($project, $collar, 20, 22, [['mineral' => 'Galena', 'abundance_pct' => 0.5]]);

        $bands = (new HoleStripTracks)->mineralization($collar)['bands'];

        $this->assertSame(['Pyrite', 'Chalcopyrite', 'Galena'], array_column($bands, 'mineral'));
        $this->assertSame([5.0, 5.0, 20.0], array_column($bands, 'from'));
        $this->assertSame([10.0, 10.0, 22.0], array_column($bands, 'to'));
        $this->assertSame([3.0, null, 0.5], array_column($bands, 'abundance_pct'));
        // Exactly the per-mineral band shape the front end already consumes.
        $this->assertSame(
            ['from', 'to', 'mineral', 'abundance_pct', 'form', 'grain_size', 'notes'],
            array_keys($bands[0]),
        );
        $this->assertSame(
            ['from' => 5.0, 'to' => 10.0, 'mineral' => 'Chalcopyrite', 'abundance_pct' => null, 'form' => null, 'grain_size' => null, 'notes' => null],
            $bands[1],
        );
        $this->assertSame('Disseminated', $bands[0]['form']);
        $this->assertSame('vein-hosted', $bands[0]['notes']);
    }

    public function test_silver_only_mineralization_is_not_drawn_until_it_is_promoted(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedSilverMineralization($collar, 5, 10, 'Pyrite', 3.0);

        $this->assertSame([], (new HoleStripTracks)->mineralization($collar)['bands']);
        $this->assertSame([], (new HoleStripTracks)->collarsWithIntervals([$collar]));

        $props = $this->workspaceProps($user, $project);
        $this->assertSame([], $props['log_hole_options'], 'silver alone does not put a hole in the picker');
    }

    public function test_a_malformed_mineralization_payload_yields_no_bands_not_an_error(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        DB::statement(
            "INSERT INTO gold.drillhole_intervals_visual (
                collar_id, workspace_id, project_id, depth_from, depth_to,
                interval_kind, mineralization_payload
             ) VALUES (?::uuid, ?::uuid, ?::uuid, 0, 5, 'mineralization', '{}'::jsonb)",
            [$collar, $this->workspaceId, $project->project_id],
        );
        $this->seedMineralization($project, $collar, 5, 10, [['mineral' => 'Pyrite']]);

        $track = (new HoleStripTracks)->mineralization($collar);

        $this->assertSame(['Pyrite'], array_column($track['bands'], 'mineral'));
        $this->assertFalse($track['truncated']);
    }

    public function test_each_track_is_bounded_and_says_when_it_was_cut(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $limit = HoleStripTracks::MAX_INTERVALS_PER_TRACK;
        DB::statement(
            "INSERT INTO gold.drillhole_intervals_visual (
                collar_id, workspace_id, project_id, depth_from, depth_to,
                interval_kind, mineralization_payload
             ) SELECT ?::uuid, ?::uuid, ?::uuid, g, g + 1, 'mineralization',
                      jsonb_build_object('minerals', jsonb_build_array(jsonb_build_object('mineral', 'Pyrite')))
               FROM generate_series(0, ?) AS g",
            [$collar, $this->workspaceId, $project->project_id, $limit],   // limit + 1 rows
        );

        $track = (new HoleStripTracks)->mineralization($collar);

        $this->assertCount($limit, $track['bands']);
        $this->assertTrue($track['truncated']);
        $this->assertFalse((new HoleStripTracks)->lithology($collar)['truncated']);
    }

    public function test_the_cap_counts_flattened_bands_so_one_crowded_interval_cannot_slip_past_it(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $limit = HoleStripTracks::MAX_INTERVALS_PER_TRACK;
        // ONE gold row, limit + 1 minerals.
        $this->seedMineralization($project, $collar, 0, 5, array_map(
            fn (int $i) => ['mineral' => 'M'.$i],
            range(0, $limit),
        ));

        $track = (new HoleStripTracks)->mineralization($collar);

        $this->assertCount($limit, $track['bands']);
        $this->assertTrue($track['truncated']);
        $this->assertSame('M0', $track['bands'][0]['mineral']);
    }

    public function test_exactly_at_the_cap_is_not_truncated(): void
    {
        ['project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $limit = HoleStripTracks::MAX_INTERVALS_PER_TRACK;
        $this->seedMineralization($project, $collar, 0, 5, array_map(
            fn (int $i) => ['mineral' => 'M'.$i],
            range(1, $limit),
        ));

        $track = (new HoleStripTracks)->mineralization($collar);

        $this->assertCount($limit, $track['bands']);
        $this->assertFalse($track['truncated']);
    }

    public function test_the_tracks_are_scoped_to_the_collar_asked_for(): void
    {
        ['project' => $project] = $this->seedProject();
        $mine = $this->seedCollar($project, 'HST-001');
        $other = $this->seedCollar($project, 'HST-002');
        $this->seedMineralization($project, $other, 0, 5, [['mineral' => 'Pyrite', 'abundance_pct' => 1.0]]);
        $this->seedLithology($project, $other, 0, 5, 'GRN', 'x', null);

        $tracks = (new HoleStripTracks)->forCollar($mine);

        $this->assertSame([], $tracks['lithology']);
        $this->assertSame([], $tracks['alteration']);
        $this->assertSame([], $tracks['mineralization']);
    }

    public function test_a_hole_with_a_logged_strip_but_no_curves_is_in_the_logs_picker_and_drawn(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $logged = $this->seedCollar($project, 'HST-LOGGED');
        $this->seedCollar($project, 'HST-BARE'); // nothing logged, no curves
        $this->seedLithology($project, $logged, 0, 5, 'GRN', 'Grey granite', null);
        $this->seedAlteration(
            $project, $logged, 0, 5,
            json_encode(['alterations' => [['type' => 'Chlorite', 'intensity' => 'Strong', 'minerals' => [], 'notes' => null]]]),
            'Chlorite (Strong)',
        );
        $this->seedMineralization($project, $logged, 2, 4, [['mineral' => 'Pyrite', 'abundance_pct' => 3.0]]);

        $props = $this->workspaceProps($user, $project);

        $this->assertSame(['HST-LOGGED'], $props['log_hole_options']);
        $this->assertSame('HST-LOGGED', $props['log_hole_id']);
        $this->assertSame([], $props['log_tracks'], 'no curves: the strip is still drawn from the geology');
        $this->assertSame(['GRN'], array_column($props['log_lithology_intervals'], 'code'));
        $this->assertSame(['Chlorite (Strong)'], array_column($props['log_alteration_intervals'], 'label'));
        $this->assertSame(['Pyrite'], array_column($props['log_mineralization_intervals'], 'mineral'));
        $this->assertFalse($props['log_tracks_truncated']['lithology']);
    }

    public function test_the_logs_panel_follows_the_requested_hole(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $a = $this->seedCollar($project, 'HST-A');
        $b = $this->seedCollar($project, 'HST-B');
        $this->seedLithology($project, $a, 0, 5, 'GRN', 'a', null);
        $this->seedLithology($project, $b, 0, 5, 'SST', 'b', null);
        $this->seedMineralization($project, $b, 1, 2, [['mineral' => 'Galena']]);

        $props = $this->workspaceProps($user, $project, '?log_hole=HST-B');

        $this->assertSame(['HST-A', 'HST-B'], $props['log_hole_options']);
        $this->assertSame('HST-B', $props['log_hole_id']);
        $this->assertSame(['SST'], array_column($props['log_lithology_intervals'], 'code'));
        $this->assertSame(['Galena'], array_column($props['log_mineralization_intervals'], 'mineral'));
    }

    public function test_the_compare_payload_carries_the_new_tracks(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedLithology($project, $collar, 0, 5, 'GRN', 'Grey granite', '#123456');
        $this->seedMineralization($project, $collar, 1, 2, [['mineral' => 'Pyrite', 'abundance_pct' => 2.5]]);

        $response = $this->actingAs($user)
            ->getJson('/projects/'.$project->slug.'/holes/HST-001/payload');

        $response->assertOk();
        $this->assertSame(['#123456'], array_column($response->json('lithology_intervals'), 'color'));
        $this->assertSame(['Pyrite'], array_column($response->json('mineralization_intervals'), 'mineral'));
        $this->assertSame([], $response->json('alteration_intervals'));
    }

    public function test_the_hole_page_receives_the_strip_tracks(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedLithology($project, $collar, 0, 5, 'GRN', 'Grey granite', null);
        $this->seedMineralization($project, $collar, 1, 2, [['mineral' => 'Pyrite', 'abundance_pct' => 2.5]]);

        $this->actingAs($user)
            ->get('/projects/'.$project->slug.'/holes/'.$collar.'/detail')
            ->assertOk()
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->component('Foundry/DrillholeDetail')
                ->has('strip_tracks.lithology', 1)
                ->has('strip_tracks.alteration', 0)
                ->has('strip_tracks.mineralization', 1)
                ->where('strip_tracks.mineralization.0.mineral', 'Pyrite')
                ->where('strip_tracks.truncated.mineralization', false));
    }

    /** The collar API is the raw record: it reads silver.mineralization, not the strip log's gold rows. */
    public function test_the_collar_api_returns_mineralization_and_alteration_notes(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProject();
        $collar = $this->seedCollar($project, 'HST-001');
        $this->seedSilverMineralization($collar, 1, 2, 'Pyrite', 2.5);

        $response = $this->actingAs($user, 'sanctum')
            ->getJson('/api/v1/projects/'.$project->project_id.'/collars/'.$collar);

        $response->assertOk();
        $row = $response->json('data') ?? $response->json();
        $this->assertSame(['Pyrite'], array_column($row['mineralization'], 'mineral'));
        $this->assertSame(2.5, $row['mineralization'][0]['abundance_pct']);
    }
}
