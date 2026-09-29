<?php

declare(strict_types=1);

namespace Tests\Feature\Models;

use App\Models\Project;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * LAR-14 (2026-09-29 audit): project slugs collided and overflowed.
 *
 * The suffix was the first 8 hex digits of a UUIDv7 project_id — the top of
 * the millisecond timestamp, constant for ~65 s — so two same-named projects
 * created close together hit `projects_slug_unique` and store() returned
 * 500. A 255-char name plus the suffix also exceeded VARCHAR(255).
 */
final class ProjectSlugTest extends TestCase
{
    use RefreshDatabase;

    private function create(string $name): Project
    {
        return Project::create([
            'project_name' => $name,
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
    }

    public function test_same_named_projects_created_in_the_same_instant_get_distinct_slugs(): void
    {
        $this->freezeTime();

        $first = $this->create('Demo');
        $second = $this->create('Demo');

        $this->assertNotSame($first->slug, $second->slug);
        $this->assertMatchesRegularExpression('/^demo-[a-z0-9]{8}$/', (string) $first->slug);
        $this->assertMatchesRegularExpression('/^demo-[a-z0-9]{8}$/', (string) $second->slug);
    }

    public function test_the_suffix_is_not_derived_from_the_time_ordered_project_id(): void
    {
        $project = $this->create('Demo');

        $this->assertStringEndsNotWith(substr((string) $project->project_id, 0, 8), (string) $project->slug);
    }

    public function test_a_maximum_length_name_still_fits_the_column(): void
    {
        $project = $this->create(str_repeat('a', 255));

        $this->assertLessThanOrEqual(Project::SLUG_MAX_LENGTH, strlen((string) $project->slug));
        $this->assertMatchesRegularExpression('/^a+-[a-z0-9]{8}$/', (string) $project->slug);
    }

    public function test_truncation_does_not_leave_a_double_hyphen(): void
    {
        // 245 chars of "a-" puts a hyphen exactly at the truncation point.
        $slug = Project::makeSlug(str_repeat('a ', 200));

        $this->assertStringNotContainsString('--', $slug);
        $this->assertLessThanOrEqual(Project::SLUG_MAX_LENGTH, strlen($slug));
    }

    public function test_a_name_with_no_sluggable_characters_still_gets_a_usable_slug(): void
    {
        $this->assertMatchesRegularExpression('/^project-[a-z0-9]{8}$/', Project::makeSlug('!!! ???'));
    }

    public function test_an_explicit_slug_is_kept(): void
    {
        $project = Project::create([
            'project_name' => 'Demo',
            'slug' => 'hand-picked',
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $this->assertSame('hand-picked', $project->slug);
    }
}
