<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\Admin\ReportBuildProgress;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * Internal — FastAPI → Laravel bridge for §15 generate_report progress.
 *
 * Service-key auth only (see VerifyServiceKey middleware). FastAPI POSTs
 * here as each section drafts / each §29 gate runs; Laravel dispatches
 * a Reverb-backed event on the private-admin.reports.{build_id} channel.
 *
 * The cockpit page subscribes via Echo and patches in place.
 */
class ReportBuildProgressController extends Controller
{
    public function broadcast(Request $request, string $build_id): JsonResponse
    {
        $payload = $request->validate([
            'stage' => ['required', 'string', 'max:60'],
            'section_id' => ['nullable', 'string', 'max:120'],
            'message' => ['nullable', 'string', 'max:500'],
            'sections_completed' => ['nullable', 'integer', 'min:0'],
            'sections_total' => ['nullable', 'integer', 'min:0'],
        ]);

        ReportBuildProgress::dispatch(
            $build_id,
            $payload['stage'],
            $payload['section_id'] ?? null,
            $payload['message'] ?? null,
            $payload['sections_completed'] ?? null,
            $payload['sections_total'] ?? null,
        );

        return response()->json(['ok' => true]);
    }
}
