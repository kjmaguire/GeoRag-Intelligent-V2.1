<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

class InboxController extends Controller
{
    public function show(Request $request): Response
    {
        $user = $request->user();
        $items = collect();

        try {
            $items = DB::table('silver.collaboration_mentions')
                ->where('user_id', $user->id)
                ->orderByDesc('created_at')
                ->limit(50)
                ->get();
        } catch (\Throwable $e) { /* */
        }

        $reviewRequests = collect();
        try {
            $reviewRequests = DB::table('silver.collaboration_review_requests')
                ->where('requested_to', $user->id)
                ->where('status', 'pending')
                ->orderByDesc('created_at')
                ->limit(20)
                ->get();
        } catch (\Throwable $e) { /* */
        }

        $refusals = collect();
        try {
            $refusals = DB::table('audit.query_audit_log')
                ->where('user_id', $user->id)
                ->whereNull('response_text')
                ->orderByDesc('created_at')
                ->limit(20)
                ->get();
        } catch (\Throwable $e) { /* */
        }

        return Inertia::render('Foundry/Inbox', [
            'mentions' => $items->map(fn ($m) => [
                'id' => (string) ($m->mention_id ?? $m->id),
                'kind' => 'mention',
                'title' => 'You were mentioned',
                'detail' => substr((string) ($m->context ?? ''), 0, 120),
                'when' => (string) $m->created_at,
            ])->values(),
            'reviews' => $reviewRequests->map(fn ($r) => [
                'id' => (string) ($r->request_id ?? $r->id),
                'kind' => 'review',
                'title' => (string) ($r->title ?? 'Review request'),
                'detail' => (string) ($r->notes ?? ''),
                'when' => (string) $r->created_at,
            ])->values(),
            'refusals' => $refusals->map(fn ($r) => [
                'id' => (string) $r->id,
                'kind' => 'refusal',
                'title' => 'Query refused',
                'detail' => substr((string) ($r->query_text ?? ''), 0, 120),
                'when' => (string) $r->created_at,
            ])->values(),
            'empty' => $items->isEmpty() && $reviewRequests->isEmpty() && $refusals->isEmpty(),
        ]);
    }
}
