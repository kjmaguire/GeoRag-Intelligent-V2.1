<?php

declare(strict_types=1);

namespace Tests\Feature\Routes;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use PHPUnit\Framework\Attributes\DataProvider;
use Symfony\Component\HttpKernel\Exception\NotFoundHttpException;
use Tests\TestCase;

/**
 * Routes for uuid columns used to be constrained with `[0-9a-f-]{36}`, which
 * also matches 36 hyphens or 36 hex digits with no structure. Such a value
 * reached Postgres as `WHERE id = '------------------------------------'`
 * (22P02) and answered 500. whereUuid() refuses to match, so it is a plain 404.
 */
final class UuidRouteConstraintTest extends TestCase
{
    use RefreshDatabase;

    private const VALID = '0190f2a1-7b3c-7d4e-8f5a-1234567890ab';

    private const HYPHENS = '------------------------------------';

    private const HEX_ONLY = 'abcdefabcdefabcdefabcdefabcdefabcdef';

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function uuidRoutes(): array
    {
        return [
            'answers' => ['GET', 'api/v1/answers/%s'],
            'maps' => ['GET', 'api/v1/maps/%s/layers'],
            'targets' => ['GET', 'api/v1/targets/%s'],
            'interpretations' => ['GET', 'api/v1/interpretations/%s'],
            'audit' => ['GET', 'api/v1/audit/%s'],
            'usage' => ['GET', 'api/v1/usage/%s'],
            'coverage-density' => ['GET', 'api/v1/projects/%s/coverage-density'],
            'conversations show' => ['GET', 'api/v1/conversations/%s'],
            'conversations upsert' => ['PUT', 'api/v1/conversations/%s'],
            'conversations destroy' => ['DELETE', 'api/v1/conversations/%s'],
            'answer-runs trust-summary' => ['GET', 'api/v1/answer-runs/%s/trust-summary'],
            'answer-runs feedback' => ['POST', 'api/v1/answer-runs/%s/feedback'],
            'ingest-progress' => ['GET', 'api/v1/ingest-progress/%s'],
            'queries result' => ['GET', 'api/v1/queries/%s/result'],
            'queries start' => ['POST', 'api/v1/queries/%s/start'],
            'queries cancel' => ['POST', 'api/v1/queries/%s/cancel'],
            'web report' => ['GET', 'projects/some-slug/reports/%s'],
            'web report figures' => ['GET', 'projects/some-slug/reports/%s/figures'],
            'web report source' => ['GET', 'projects/some-slug/reports/%s/source'],
        ];
    }

    #[DataProvider('uuidRoutes')]
    public function test_a_structureless_36_character_segment_does_not_match_the_route(string $method, string $uriTemplate): void
    {
        foreach ([self::HYPHENS, self::HEX_ONLY] as $bad) {
            try {
                Route::getRoutes()->match(Request::create('/'.sprintf($uriTemplate, $bad), $method));
                $this->fail("{$method} {$uriTemplate} matched the non-UUID '{$bad}'");
            } catch (NotFoundHttpException) {
                $this->addToAssertionCount(1);
            }
        }
    }

    #[DataProvider('uuidRoutes')]
    public function test_a_real_uuid_still_matches_in_either_case(string $method, string $uriTemplate): void
    {
        foreach ([self::VALID, strtoupper(self::VALID)] as $good) {
            $route = Route::getRoutes()->match(Request::create('/'.sprintf($uriTemplate, $good), $method));
            $this->assertNotNull($route);
        }
    }

    public function test_a_36_hyphen_segment_answers_404_not_500_over_http(): void
    {
        $this->actingAs(User::factory()->create(), 'sanctum')
            ->getJson('/api/v1/answers/'.self::HYPHENS)
            ->assertNotFound();

        $this->actingAs(User::factory()->create(), 'sanctum')
            ->getJson('/api/v1/ingest-progress/'.self::HYPHENS)
            ->assertNotFound();
    }
}
