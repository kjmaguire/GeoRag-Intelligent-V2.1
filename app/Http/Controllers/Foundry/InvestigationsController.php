<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

class InvestigationsController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $conversations = DB::table('public.chat_conversations')
            ->where('project_id', $project->project_id)
            ->orderByDesc('updated_at')
            ->limit(50)
            ->get();

        return Inertia::render('Foundry/Investigations', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'investigations' => $conversations->map(fn ($c) => [
                'id' => (string) $c->conversation_id,
                'title' => (string) ($c->title ?? 'Untitled investigation'),
                'updated' => (string) ($c->updated_at ?? ''),
                'pinned' => false,
            ])->values(),
            'empty' => $conversations->isEmpty(),
        ]);
    }
}
