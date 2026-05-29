<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/ChatController — project-scoped chat surface.
 *
 * Lives at /projects/{slug}/chat. Reads public.chat_conversations + chat_messages
 * filtered to the active project. The legacy /chat top-level route is gone —
 * chat only exists inside a project context now.
 */
class ChatController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $user = $request->user();

        $threads = DB::table('public.chat_conversations')
            ->where('user_id', $user->id)
            ->where('project_id', $project->project_id)
            ->orderByDesc('updated_at')
            ->limit(50)
            ->get();

        $activeId = $request->query('thread');
        $activeMessages = collect();
        $activeThread = null;
        if ($activeId) {
            $activeThread = $threads->firstWhere('conversation_id', $activeId);
            $activeMessages = DB::table('public.chat_messages')
                ->where('conversation_id', $activeId)
                ->orderBy('created_at')
                ->limit(200)
                ->get();
        } elseif ($threads->isNotEmpty()) {
            $activeId = (string) $threads->first()->conversation_id;
            $activeThread = $threads->first();
            $activeMessages = DB::table('public.chat_messages')
                ->where('conversation_id', $activeId)
                ->orderBy('created_at')
                ->limit(200)
                ->get();
        }

        // Phase 3 / Step 3.2 — surface the active project's context so the
        // query-builder UI can pre-populate smart defaults (CRS, jurisdiction
        // / region). Other defaults (active data sources) lean on
        // commodity / status when relevant; the UI treats missing values as
        // unspecified per the Phase 2.4 contract.
        $projectCrsEpsg = $this->parseEpsgFromCrsDatum($project->crs_datum ?? null);

        return Inertia::render('Foundry/Chat', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
                'crs_datum' => $project->crs_datum,
                'crs_epsg' => $projectCrsEpsg,
                'region' => $project->region,
                'commodity' => $project->commodity,
            ],
            'threads' => $threads->map(fn ($t) => [
                'id' => (string) $t->conversation_id,
                'title' => (string) ($t->title ?? 'Untitled thread'),
                'updated' => isset($t->updated_at) ? (string) $t->updated_at : '',
            ])->values(),
            'active_thread_id' => $activeId,
            'active_thread' => $activeThread ? [
                'id' => (string) $activeThread->conversation_id,
                'title' => (string) ($activeThread->title ?? 'Untitled thread'),
            ] : null,
            'messages' => $activeMessages->map(function ($m) {
                $meta = is_string($m->metadata ?? null) ? json_decode($m->metadata, true) : ($m->metadata ?? null);

                return [
                    'id' => (string) $m->message_id,
                    'role' => (string) $m->role,
                    'content' => (string) $m->content,
                    'created_at' => (string) $m->created_at,
                    'anaphora' => $meta['anaphora'] ?? null,
                    'answer_run_id' => $meta['answer_run_id'] ?? null,
                    'citations' => $meta['citations'] ?? [],
                    'confidence' => $meta['confidence'] ?? null,
                ];
            })->values(),
            'empty' => $threads->isEmpty(),
            'legacy_url' => '/chat',
        ]);
    }

    /**
     * Parse "EPSG:NNNN" style strings into the integer code. Returns null
     * for malformed or unset values; the query-builder UI surfaces this as
     * "unspecified" so the geologist sees what the default is doing.
     */
    private function parseEpsgFromCrsDatum(?string $crsDatum): ?int
    {
        if ($crsDatum === null || $crsDatum === '') {
            return null;
        }
        if (preg_match('/^EPSG:(\d+)$/i', trim($crsDatum), $matches) === 1) {
            $epsg = (int) $matches[1];
            if ($epsg >= 1024 && $epsg <= 32767) {
                return $epsg;
            }
        }

        return null;
    }
}
