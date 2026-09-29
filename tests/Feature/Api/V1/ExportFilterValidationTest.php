<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;
use Illuminate\Testing\TestResponse;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * LAR-9 (2026-09-29 audit): open-ended export ranges were rejected.
 *
 * `gt:filters.min_depth` (and the same shape for from_depth and the drill
 * dates) fails when the referenced field is absent, so
 * `{"filters": {"max_depth": 500}}` got a 422 "must be greater than
 * filters.min_depth". The comparison now runs only when both ends are given.
 */
final class ExportFilterValidationTest extends TestCase
{
    use RefreshDatabase;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name' => 'Export Filter Validation '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $user = User::factory()->create();
        $user->projects()->attach($this->project->project_id, ['role' => 'owner']);
        $this->actingAs($user);

        Queue::fake();
    }

    /**
     * @param array<string, mixed> $filters
     */
    private function store(array $filters, string $type = 'csv_collars'): TestResponse
    {
        return $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => $type, 'filters' => $filters],
        );
    }

    /**
     * @return array<string, array{0: array<string, mixed>}>
     */
    public static function openEndedRanges(): array
    {
        return [
            'max_depth alone' => [['max_depth' => 500]],
            'min_depth alone' => [['min_depth' => 100]],
            'from_depth_max alone' => [['from_depth_max' => 50]],
            'drill_date_to alone' => [['drill_date_to' => '2024-12-31']],
            'both ends, ordered' => [['min_depth' => 100, 'max_depth' => 500, 'drill_date_from' => '2024-01-01', 'drill_date_to' => '2024-01-01']],
        ];
    }

    /**
     * @param array<string, mixed> $filters
     */
    #[DataProvider('openEndedRanges')]
    public function test_open_ended_and_ordered_ranges_are_accepted(array $filters): void
    {
        $this->store($filters)->assertStatus(202);
    }

    public function test_an_inverted_depth_range_is_still_rejected(): void
    {
        $this->store(['min_depth' => 500, 'max_depth' => 100])
            ->assertStatus(422)
            ->assertJsonValidationErrors(['filters.max_depth']);
    }

    public function test_an_equal_depth_range_is_rejected(): void
    {
        $this->store(['from_depth_min' => 10, 'from_depth_max' => 10], 'csv_samples')
            ->assertStatus(422)
            ->assertJsonValidationErrors(['filters.from_depth_max']);
    }

    public function test_an_inverted_date_range_is_rejected(): void
    {
        $this->store(['drill_date_from' => '2024-06-01', 'drill_date_to' => '2024-01-01'])
            ->assertStatus(422)
            ->assertJsonValidationErrors(['filters.drill_date_to']);
    }

    public function test_a_negative_max_depth_is_rejected(): void
    {
        $this->store(['max_depth' => -5])
            ->assertStatus(422)
            ->assertJsonValidationErrors(['filters.max_depth']);
    }
}
