<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Master-plan §10-v2 — Golden Question Authoring UI (doc-phase 179).
 *
 * Admin-only surface for creating, editing, transitioning, and dry-running
 * golden questions in eval.golden_questions. The actual CRUD + dry-run logic
 * lives in FastAPI under /api/v1/admin/eval/questions; this controller is a
 * thin proxy that handles auth, validation shape, and Inertia rendering.
 *
 * Routes:
 *   GET    /admin/eval/questions             — index (paginated list + filters)
 *   GET    /admin/eval/questions/{id}        — detail / edit
 *   POST   /admin/eval/questions             — create draft
 *   PUT    /admin/eval/questions/{id}        — update existing
 *   POST   /admin/eval/questions/{id}/transition — flip status
 *   POST   /admin/eval/questions/{id}/dry-run    — run synthetic evaluator
 *
 * Auth: 'admin' Gate (users.is_admin = true).
 */
class EvalQuestionsController extends Controller
{
    private const VALID_SETS = [
        'core_chat', 'public_private_boundary', 'numeric_grounding',
        'refusal_correctness', 'target_recommendation', 'report_section',
        'schema_mapping', 'ocr_triage',
    ];

    private const VALID_DIFFICULTIES = ['easy', 'medium', 'hard'];

    private const VALID_STATUSES = ['draft', 'active', 'retired'];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $request->validate([
            'question_set' => ['nullable', 'in:'.implode(',', self::VALID_SETS)],
            'status' => ['nullable', 'in:'.implode(',', self::VALID_STATUSES)],
            'search' => ['nullable', 'string', 'max:200'],
            'limit' => ['nullable', 'integer', 'min:1', 'max:200'],
            'offset' => ['nullable', 'integer', 'min:0'],
        ]);

        $list = $this->fastapiGet('/api/v1/admin/eval/questions', $filters);

        return Inertia::render('Admin/EvalQuestions', [
            'questions' => $list['items'] ?? [],
            'total' => $list['total'] ?? 0,
            'filters' => array_filter($filters, fn ($v) => $v !== null),
            'valid_sets' => self::VALID_SETS,
            'valid_difficulties' => self::VALID_DIFFICULTIES,
            'valid_statuses' => self::VALID_STATUSES,
        ]);
    }

    public function show(Request $request, string $id): Response
    {
        $this->authorize('admin');

        $question = $this->fastapiGet("/api/v1/admin/eval/questions/{$id}");

        return Inertia::render('Admin/EvalQuestionEditor', [
            'question' => $question,
            'valid_sets' => self::VALID_SETS,
            'valid_difficulties' => self::VALID_DIFFICULTIES,
            'valid_statuses' => self::VALID_STATUSES,
        ]);
    }

    public function create(Request $request): Response
    {
        $this->authorize('admin');

        // Default skeleton mirrors the FastAPI UpsertQuestionRequest defaults.
        return Inertia::render('Admin/EvalQuestionEditor', [
            'question' => null,
            'valid_sets' => self::VALID_SETS,
            'valid_difficulties' => self::VALID_DIFFICULTIES,
            'valid_statuses' => self::VALID_STATUSES,
        ]);
    }

    public function store(Request $request): RedirectResponse
    {
        $this->authorize('admin');

        $payload = $this->validateUpsert($request);
        $payload['authored_by_user_id'] = (int) $request->user()->id;

        $created = $this->fastapiPost('/api/v1/admin/eval/questions', $payload);

        return redirect()
            ->route('admin.eval.questions.show', ['id' => $created['question_id']])
            ->with('success', 'Question created as draft.');
    }

    public function update(Request $request, string $id): RedirectResponse
    {
        $this->authorize('admin');

        $payload = $this->validateUpsert($request);
        $payload['authored_by_user_id'] = (int) $request->user()->id;

        $this->fastapiPut("/api/v1/admin/eval/questions/{$id}", $payload);

        return redirect()
            ->route('admin.eval.questions.show', ['id' => $id])
            ->with('success', 'Question updated.');
    }

    public function transition(Request $request, string $id): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'status' => ['required', 'in:'.implode(',', self::VALID_STATUSES)],
        ]);
        // Reviewer = current user; FastAPI enforces reviewer != author.
        $payload['reviewer_user_id'] = (int) $request->user()->id;

        return response()->json(
            $this->fastapiPost("/api/v1/admin/eval/questions/{$id}/transition", $payload),
        );
    }

    public function dryRun(Request $request, string $id): JsonResponse
    {
        $this->authorize('admin');

        return response()->json(
            $this->fastapiPost("/api/v1/admin/eval/questions/{$id}/dry-run", []),
        );
    }

    /**
     * @return array<string, mixed>
     */
    private function validateUpsert(Request $request): array
    {
        return $request->validate([
            'question_set' => ['required', 'in:'.implode(',', self::VALID_SETS)],
            'question_text' => ['required', 'string', 'min:5', 'max:2000'],
            'context_setup' => ['nullable', 'array'],
            'expected_intent_class' => ['nullable', 'string', 'max:60'],
            'expected_citations' => ['nullable', 'array'],
            'expected_entities' => ['nullable', 'array'],
            'expected_numeric_values' => ['nullable', 'array'],
            'expected_refusal' => ['boolean'],
            'expected_refusal_reason' => ['nullable', 'string', 'max:1000'],
            'expected_language_compliance' => ['nullable', 'array'],
            'difficulty' => ['required', 'in:'.implode(',', self::VALID_DIFFICULTIES)],
        ]);
    }

    private function fastapiBase(): string
    {
        return rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );
    }

    private function serviceKey(): string
    {
        $key = (string) config('services.fastapi.service_key', '');
        if ($key === '') {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }
        return $key;
    }

    /**
     * @param  array<string, mixed>  $query
     * @return array<string, mixed>
     */
    private function fastapiGet(string $path, array $query = []): array
    {
        $resp = Http::withHeaders(['X-Service-Key' => $this->serviceKey()])
            ->timeout(15)
            ->get($this->fastapiBase().$path, $query);
        if (! $resp->ok()) {
            abort($resp->status(), $resp->body());
        }
        return $resp->json() ?? [];
    }

    /**
     * @param  array<string, mixed>  $body
     * @return array<string, mixed>
     */
    private function fastapiPost(string $path, array $body): array
    {
        $resp = Http::withHeaders(['X-Service-Key' => $this->serviceKey()])
            ->timeout(30)
            ->post($this->fastapiBase().$path, $body);
        if (! $resp->ok()) {
            abort($resp->status(), $resp->body());
        }
        return $resp->json() ?? [];
    }

    /**
     * @param  array<string, mixed>  $body
     * @return array<string, mixed>
     */
    private function fastapiPut(string $path, array $body): array
    {
        $resp = Http::withHeaders(['X-Service-Key' => $this->serviceKey()])
            ->timeout(30)
            ->put($this->fastapiBase().$path, $body);
        if (! $resp->ok()) {
            abort($resp->status(), $resp->body());
        }
        return $resp->json() ?? [];
    }
}
