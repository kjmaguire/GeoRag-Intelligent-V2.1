<?php

declare(strict_types=1);

namespace App\Providers;

use App\Services\Citations\CitationResolverRegistry;
use App\Services\Citations\Resolvers\AssayResolver;
use App\Services\Citations\Resolvers\CollarsResolver;
use App\Services\Citations\Resolvers\LithologyResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\AssessmentSurveyResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\DrillholeResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\MineralDispositionResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\MineralOccurrenceResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\MineResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\ResourcePotentialResolver;
use App\Services\Citations\Resolvers\PublicGeoscience\RockSampleResolver;
use App\Services\Citations\Resolvers\ReportResolver;
use App\Services\Citations\Resolvers\SamplesResolver;
use Illuminate\Support\ServiceProvider;

/**
 * Wires every concrete `CitationResolver` into the `CitationResolverRegistry`
 * so `CitationController` can dispatch by `source_chunk_id` prefix.
 *
 * Adding a new source type is a two-step process:
 *   1. Implement `CitationResolver` (or extend a relevant abstract base) in
 *      `app/Services/Citations/Resolvers/`.
 *   2. Register it in the closure below.
 *
 * That's it — no edit to the controller, no edit to the dispatcher.
 *
 * Octane safety
 * -------------
 * The registry is a singleton (one instance per worker, shared across
 * requests). Resolvers are stateless and use request-scoped facades
 * (DB::, Cache::), so the singleton holds no per-request state across
 * requests. Per CLAUDE.md hard rule #3, we explicitly do NOT inject the
 * container, request, or config repository into the registry's
 * constructor.
 */
final class CitationResolverServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        // Octane-safety note (Eval 19 audit, 2026-05-20):
        // singleton() is intentional here. The registry holds a
        // prefix→resolver map populated at boot. Resolvers are
        // stateless — each `resolve()` call reads request-scoped
        // facades (DB::, Auth::) which Octane already resets between
        // requests. No workspace_id or user_id is captured at
        // construction. Do NOT flip to scoped() — that would re-run
        // the registration list on every request for no gain.
        $this->app->singleton(CitationResolverRegistry::class, function (): CitationResolverRegistry {
            $registry = new CitationResolverRegistry;

            // Silver / report resolvers (5)
            $registry->register(new ReportResolver);
            $registry->register(new CollarsResolver);
            $registry->register(new LithologyResolver);
            $registry->register(new SamplesResolver);
            // 2026-05-20 drillhole schema — assays_v2 wide-form table.
            $registry->register(new AssayResolver);

            // Public Geoscience resolvers (7)
            $registry->register(new MineResolver);
            $registry->register(new MineralOccurrenceResolver);
            $registry->register(new DrillholeResolver);
            $registry->register(new ResourcePotentialResolver);
            $registry->register(new RockSampleResolver);
            $registry->register(new AssessmentSurveyResolver);
            $registry->register(new MineralDispositionResolver);

            return $registry;
        });
    }
}
