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

            // Plan §4d — share the guard-error i18n catalog with the React
            // side so client-side code (e.g. the GuardErrorMessage primitive)
            // can render template strings without a server round-trip. Map
            // is small (~20 keys × ~150 chars = ~3 KB) so it's cheap to
            // include on every Inertia response. Lazy closure keeps it off
            // partial reloads that don't request it.
            'guard_errors' => fn () => trans('guard_errors'),

            // ── Foundry shell rail data (project-scoped lists) ───────────────
            // These hydrate FoundryShell's left rail (project-scoped chat threads
            // and saved map views) and the top-bar badge counts (inbox + reviews).
            // All are lazy closures so they only run on requests that need them
            // (Inertia partial reloads will skip ones that aren't selected).

            'project_threads' => fn () => $this->resolveProjectThreads($request),
            'project_saved_views' => fn () => $this->resolveSavedViews($request),
            'inbox_count' => fn () => $this->resolveInboxCount($request),
            'review_count' => fn () => $this->resolveReviewCount($request),
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

    /**
     * @return list<array{id:string,name:string,scope:string}>
     */
    private function resolveSavedViews(Request $request): array
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
            return DB::table('silver.saved_map_views')
                ->where(function ($q) use ($projectId, $user) {
                    $q->where('project_id', $projectId)->orWhere('created_by', $user->id);
                })
                ->orderByDesc('updated_at')
                ->limit(20)
                ->get(['view_id', 'name', 'project_id', 'workspace_id'])
                ->map(function ($v) {
                    $scope = 'user';
                    if (isset($v->workspace_id) && $v->workspace_id !== null && $v->project_id === null) {
                        $scope = 'workspace';
                    } elseif (isset($v->project_id) && $v->project_id !== null) {
                        $scope = 'project';
                    }

                    return [
                        'id' => (string) $v->view_id,
                        'name' => (string) ($v->name ?? 'Untitled view'),
                        'scope' => $scope,
                    ];
                })->all();
        } catch (\Throwable $e) {
            return [];
        }
    }

    private function resolveInboxCount(Request $request): int
    {
        $user = $request->user();
        if (! $user) {
            return 0;
        }
        try {
            return (int) DB::table('silver.collaboration_mentions')
                ->where('user_id', $user->id)
                ->whereNull('read_at')
                ->count();
        } catch (\Throwable $e) {
            return 0;
        }
    }

    private function resolveReviewCount(Request $request): int
    {
        $user = $request->user();
        if (! $user) {
            return 0;
        }
        try {
            return (int) DB::table('silver.collaboration_review_requests')
                ->where('requested_to', $user->id)
                ->where('status', 'pending')
                ->count();
        } catch (\Throwable $e) {
            return 0;
        }
    }
}
