<?php

namespace Tests\Feature;

use App\Models\Project;
use App\Models\QueryAuditLog;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * D5 — golden-set report generator.
 *
 * Uses the A4 query_text_hash aggregation to surface recurring struggling
 * queries that should be promoted into test_golden_queries.py. These tests
 * verify the scoring, filtering, and formatting logic.
 */
class GoldenSetReportTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');
    }

    private function seedQuery(string $query, ?float $confidence, int $times = 1, int $minutesAgo = 1): void
    {
        for ($i = 0; $i < $times; $i++) {
            QueryAuditLog::create([
                'user_id'       => null,
                'project_id'    => (string) Str::uuid(),
                'query_id'      => (string) Str::uuid(),
                'query_text'    => $query,
                'confidence'    => $confidence,
                'ip_address'    => '127.0.0.1',
                'llm_model'     => 'test-model',
                'created_at'    => now()->subMinutes($minutesAgo),
            ]);
        }
    }

    public function test_writes_report_with_top_candidates(): void
    {
        // Three query groups, varying weight.
        $this->seedQuery('what deposit?', 0.15, times: 6);  // high weight: 6 × 0.85 = 5.1
        $this->seedQuery('show holes', 0.85, times: 3);     // low weight:  3 × 0.15 = 0.45
        $this->seedQuery('what grade?', 0.30, times: 4);    // medium:      4 × 0.70 = 2.8

        $outputPath = sys_get_temp_dir() . '/golden-' . Str::uuid() . '.md';
        register_shutdown_function(fn () => @unlink($outputPath));

        $this->artisan('audit:golden-set-report', [
            '--since' => '1d',
            '--limit' => 5,
            '--min-count' => 2,
            '--output' => $outputPath,
        ])->assertSuccessful();

        $this->assertFileExists($outputPath);
        $content = file_get_contents($outputPath);

        // All three groups qualify (count >= 2) and appear in weight order.
        $this->assertStringContainsString('what deposit?', $content);
        $this->assertStringContainsString('what grade?', $content);
        $this->assertStringContainsString('show holes', $content);

        $posDeposit = strpos($content, 'what deposit?');
        $posGrade = strpos($content, 'what grade?');
        $posShow = strpos($content, 'show holes');
        $this->assertLessThan($posGrade, $posDeposit, 'highest-weight entry should appear first');
        $this->assertLessThan($posShow, $posGrade, 'medium-weight should precede low-weight');

        // Weight line reflects the score.
        $this->assertMatchesRegularExpression(
            '/\*\*weight\*\*: \d+/',
            $content,
        );
    }

    public function test_respects_min_count_filter(): void
    {
        // Only one occurrence — below min-count.
        $this->seedQuery('lonely query', 0.1, times: 1);

        $outputPath = sys_get_temp_dir() . '/golden-' . Str::uuid() . '.md';
        register_shutdown_function(fn () => @unlink($outputPath));

        $this->artisan('audit:golden-set-report', [
            '--min-count' => 3,
            '--output' => $outputPath,
        ])->assertSuccessful();

        $content = file_get_contents($outputPath);
        $this->assertStringNotContainsString('lonely query', $content);
        $this->assertStringContainsString('No qualifying queries', $content);
    }

    public function test_action_suggestion_tiers(): void
    {
        // Very low confidence + high count → hallucination-failure suggestion.
        $this->seedQuery('fabricated numbers?', 0.1, times: 5);

        $outputPath = sys_get_temp_dir() . '/golden-' . Str::uuid() . '.md';
        register_shutdown_function(fn () => @unlink($outputPath));

        $this->artisan('audit:golden-set-report', [
            '--min-count' => 2,
            '--output' => $outputPath,
        ])->assertSuccessful();

        $content = file_get_contents($outputPath);
        $this->assertStringContainsString('test_hallucination_failures.py', $content);
    }

    public function test_reject_bad_since_format(): void
    {
        $this->artisan('audit:golden-set-report', [
            '--since' => 'not-a-duration',
        ])->assertFailed();
    }
}
