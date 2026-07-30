<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Middleware;

class HandleInertiaRequests extends Middleware
{
    /**
     * The root template that's loaded on the first page visit.
     *
     * @see https://inertiajs.com/server-side-setup#root-template
     *
     * @var string
     */
    protected $rootView = 'app';

    /**
     * Determines the current asset version.
     *
     * @see https://inertiajs.com/asset-versioning
     */
    public function version(Request $request): ?string
    {
        return parent::version($request);
    }

    /**
     * Define the props that are shared by default.
     *
     * @see https://inertiajs.com/shared-data
     *
     * @return array<string, mixed>
     */
    public function share(Request $request): array
    {
        return [
            ...parent::share($request),
            'auth' => [
                'user' => $request->user() ? [
                    'id' => $request->user()->id,
                    'name' => $request->user()->name,
                    'email' => $request->user()->email,
                    'is_admin' => (bool) ($request->user()->is_admin ?? false),
                ] : null,
            ],
            'flash' => [
                'success' => fn () => $request->session()->get('success'),
                'error' => fn () => $request->session()->get('error'),
            ],
            'app' => [
                'env' => app()->environment(),
                'debug' => (bool) config('app.debug'),
            ],
            'basemap_styles' => config('services.basemap.styles'),

            // Foundry shell project-scoped chat threads.
            'project_threads' => fn () => $this->resolveProjectThreads($request),
        ];
    }

    /**
     * Extract the project slug from the current URL when the user is on a
     * /projects/{slug}/... route. Returns null otherwise. Lookup-side guards
     * keep the rail lists empty when the user isn't in a project context.
     */
    private function currentProjectId(Request $request): ?string
    {
        $path = $request->path();
        if (! preg_match('#^projects/([a-z0-9\-]+)(/|$)#', $path, $matches)) {
            return null;
        }
        $slug = $matches[1];
        if ($slug === 'new') {
            return null;
        }
        try {
            $row = DB::table('silver.projects')->where('slug', $slug)->select('project_id')->first();

            return $row ? (string) $row->project_id : null;
        } catch (\Throwable $e) {
            return null;
        }
    }

    /**
     * @return list<array{id:string,title:string,updated:string}>
     */
    private function resolveProjectThreads(Request $request): array
    {
        $user = $request->user();
        if (! $user) {
            return [];
        }
        $projectId = $this->currentProjectId($request);
        if (! $projectId) {
            return [];
        }
        try {
            return DB::table('public.chat_conversations')
                ->where('user_id', $user->id)
                ->where('project_id', $projectId)
                ->orderByDesc('updated_at')
                ->limit(20)
                ->get(['conversation_id', 'title', 'updated_at'])
                ->map(fn ($t) => [
                    'id' => (string) $t->conversation_id,
                    'title' => (string) ($t->title ?? 'Untitled thread'),
                    'updated' => isset($t->updated_at) ? (string) $t->updated_at : '',
                ])->all();
        } catch (\Throwable $e) {
            return [];
        }
    }
}
