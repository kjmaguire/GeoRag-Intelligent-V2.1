<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use App\Services\Audit\AuditEmitter;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\DB;

/**
 * Sender + per-flow JWT key rotation actions for the external-webhook bridge.
 *
 * Narrowed 2026-07-28 (A7): this class used to also serve the
 * `/admin/integrations` Kestra dashboard (a Kestra-side flow listing +
 * Hatchet run rollup + feature-flag toggle UI). That page
 * (resources/js/Pages/Admin/Integrations.tsx) was deleted in the reader-core
 * trim, and with it the `GET /admin/integrations` route this class's
 * `index()`/`toggleFlag()` methods and their ~9 private helpers existed to
 * serve. Removed along with the dead route(s).
 *
 * That deletion had already silently orphaned the four methods below: each
 * redirected to `route('admin.integrations')`, a route name nothing
 * registers any more, so every successful sender-toggle / key-rotation /
 * sender-registration / HMAC-rotation ended in a RouteNotFoundException
 * AFTER the database mutation had already committed. Confirmed via
 * `artisan route:list --name=admin.integrations` (zero matches) before this
 * fix. Now `redirect()->back()`.
 *
 * Auth: 'admin' Gate.
 */
class IntegrationsController extends Controller
{
    public function toggleSender(Request $request, string $id, string $action): RedirectResponse
    {
        $this->authorize('admin');

        if (preg_match('/^[0-9a-fA-F-]{36}$/', $id) !== 1) {
            abort(404);
        }
        if ($action !== 'disable' && $action !== 'enable') {
            abort(404);
        }

        $update = $action === 'disable'
            ? 'disabled_at = clock_timestamp()'
            : 'disabled_at = NULL';
        DB::connection('pgsql')->statement(
            "UPDATE usage.external_notification_senders SET $update WHERE id = ?::uuid",
            [$id],
        );

        return redirect()->back()->with(
            'flash', sprintf('sender %s %sd', substr($id, 0, 8), $action),
        );
    }

    public function rotateFlowKey(Request $request): RedirectResponse
    {
        $this->authorize('admin');

        $validated = $request->validate([
            'flow_name' => ['required', 'string', 'regex:/^[a-z][a-z0-9_]{0,63}$/'],
            'overlap_hours' => ['required', 'integer', 'min:0', 'max:168'],
        ]);

        $flowName = (string) $validated['flow_name'];
        $overlapHours = (int) $validated['overlap_hours'];

        $exists = DB::connection('pgsql')->selectOne(
            'SELECT 1 AS ok FROM workflow.flow_registry WHERE flow_name = ? LIMIT 1',
            [$flowName],
        );
        if ($exists === null) {
            abort(404, "unknown flow_name: {$flowName}");
        }

        $encKey = (string) env('AUDIT_ENCRYPTION_KEY', '');
        if ($encKey === '') {
            abort(503, 'AUDIT_ENCRYPTION_KEY not configured server-side');
        }

        $kid = 'rotated-'.time();
        $secret = bin2hex(random_bytes(32));

        // Phase 10 Step 1 (R-P9-1) — capture the prior active kid
        // BEFORE rotation so the audit payload records the
        // before/after pair.
        $priorKidRow = DB::connection('pgsql')->selectOne(
            <<<'SQL'
            SELECT kid FROM workflow.flow_jwt_keys
             WHERE flow_name = ?
               AND valid_until IS NULL
             ORDER BY valid_from DESC
             LIMIT 1
            SQL,
            [$flowName],
        );
        $priorKid = $priorKidRow?->kid;

        // set_config(..., true) is txn-local in PG; wrap the GUC seed
        // + the SECURITY DEFINER call in one transaction so the
        // function call sees the encryption key.
        DB::connection('pgsql')->transaction(static function () use ($encKey, $flowName, $kid, $secret, $overlapHours): void {
            DB::connection('pgsql')->statement(
                "SELECT set_config('app.audit_encryption_key', ?, true)",
                [$encKey],
            );
            DB::connection('pgsql')->statement(
                'SELECT workflow.set_flow_jwt_secret(?, ?, ?, ?)',
                [$flowName, $kid, $secret, $overlapHours],
            );
        });

        // Phase 10 Step 1 — audit ledger emission. Secret itself is
        // NEVER serialised into the payload; only kid metadata + who
        // requested the rotation lands in the ledger.
        app(AuditEmitter::class)->emit(
            actionType: 'workflow.jwt_key.rotated',
            actorId: Auth::id(),
            actorKind: AuditEmitter::ACTOR_USER,
            targetSchema: 'workflow',
            targetTable: 'flow_jwt_keys',
            targetId: $flowName,
            payload: [
                'flow_name' => $flowName,
                'prior_kid' => $priorKid,
                'new_kid' => $kid,
                'overlap_hours' => $overlapHours,
            ],
        );

        return redirect()->back()->with(
            'flash',
            sprintf(
                'rotated %s → kid=%s (overlap=%dh)',
                $flowName,
                $kid,
                $overlapHours,
            ),
        );
    }

    public function registerSender(Request $request): RedirectResponse
    {
        $this->authorize('admin');

        $validated = $request->validate([
            'source' => ['required', 'string', 'regex:/^[a-z][a-z0-9_\-]{1,63}$/'],
            'description' => ['nullable', 'string', 'max:255'],
        ]);

        $source = (string) $validated['source'];
        $description = $validated['description'] ?? null;

        $encKey = (string) env('AUDIT_ENCRYPTION_KEY', '');
        if ($encKey === '') {
            abort(503, 'AUDIT_ENCRYPTION_KEY not configured server-side');
        }

        // Reject if a sender with this source already exists — keeps
        // the operator from accidentally clobbering an active sender
        // (rotation is a separate flow).
        $existing = DB::connection('pgsql')->selectOne(
            'SELECT id::text AS id FROM usage.external_notification_senders WHERE source = ? LIMIT 1',
            [$source],
        );
        if ($existing !== null) {
            return redirect()->back()->with(
                'flash',
                sprintf('sender %s already exists (id=%s) — use rotate instead', $source, substr($existing->id, 0, 8)),
            );
        }

        $secret = bin2hex(random_bytes(32));
        $kid = 'primary';

        $senderId = null;
        DB::connection('pgsql')->transaction(function () use ($encKey, $source, $kid, $secret, $description, &$senderId): void {
            DB::connection('pgsql')->statement(
                "SELECT set_config('app.audit_encryption_key', ?, true)",
                [$encKey],
            );
            $row = DB::connection('pgsql')->selectOne(
                'SELECT usage.register_external_notification_sender(?, ?, ?, ?, NULL) AS id',
                [$source, $kid, $secret, $description],
            );
            $senderId = (string) $row->id;
        });

        // Audit emission — secret never lands in the payload.
        app(AuditEmitter::class)->emit(
            actionType: 'usage.external_notification_sender.registered',
            actorId: Auth::id(),
            actorKind: AuditEmitter::ACTOR_USER,
            targetSchema: 'usage',
            targetTable: 'external_notification_senders',
            targetId: $senderId,
            payload: [
                'source' => $source,
                'kid' => $kid,
                'description' => $description,
            ],
        );

        // Flash the secret ONCE so the operator can copy it.
        // The secret never persists past the next page load.
        return redirect()->back()->with([
            'flash' => sprintf('registered %s (kid=%s)', $source, $kid),
            'sender_secret' => $secret,
            'sender_source' => $source,
        ]);
    }

    public function rotateSenderHmac(Request $request, string $id): RedirectResponse
    {
        $this->authorize('admin');

        if (preg_match('/^[0-9a-fA-F-]{36}$/', $id) !== 1) {
            abort(404);
        }

        // Phase 14 Step 2 (R-P12-l6-overlap-hmac) — optional overlap
        // window. 0 = immediate cut (the Phase 12 Step 4 default).
        // Bounded to 168h (one week) to match the JWT rotate cap.
        $validated = $request->validate([
            'overlap_hours' => ['sometimes', 'integer', 'min:0', 'max:168'],
        ]);
        $overlapHours = (int) ($validated['overlap_hours'] ?? 0);

        $prior = DB::connection('pgsql')->selectOne(
            <<<'SQL'
            SELECT id::text AS id, source, secret_kid
              FROM usage.external_notification_senders
             WHERE id = ?::uuid AND disabled_at IS NULL
             LIMIT 1
            SQL,
            [$id],
        );
        if ($prior === null) {
            abort(404, "sender not found or already disabled: {$id}");
        }

        $encKey = (string) env('AUDIT_ENCRYPTION_KEY', '');
        if ($encKey === '') {
            abort(503, 'AUDIT_ENCRYPTION_KEY not configured server-side');
        }

        $secret = bin2hex(random_bytes(32));
        $kid = 'rotated-'.time();

        $newId = null;
        DB::connection('pgsql')->transaction(function () use ($encKey, $prior, $kid, $secret, $overlapHours, &$newId): void {
            DB::connection('pgsql')->statement(
                "SELECT set_config('app.audit_encryption_key', ?, true)",
                [$encKey],
            );
            $row = DB::connection('pgsql')->selectOne(
                'SELECT usage.register_external_notification_sender(?, ?, ?, NULL, ?::uuid) AS id',
                [$prior->source, $kid, $secret, $prior->id],
            );
            $newId = (string) $row->id;
            // Phase 14 Step 2 — schedule the prior row's disable for
            // now() + overlap_hours. When overlap=0 the behaviour
            // matches Phase 12 Step 4 (immediate cut).
            DB::connection('pgsql')->statement(
                'UPDATE usage.external_notification_senders '
                .'SET disabled_at = clock_timestamp() + make_interval(hours => ?) '
                .'WHERE id = ?::uuid',
                [$overlapHours, $prior->id],
            );
        });

        // Audit emission — secret NEVER lands in the payload.
        app(AuditEmitter::class)->emit(
            actionType: 'usage.external_notification_sender.hmac_rotated',
            actorId: Auth::id(),
            actorKind: AuditEmitter::ACTOR_USER,
            targetSchema: 'usage',
            targetTable: 'external_notification_senders',
            targetId: $newId,
            payload: [
                'source' => $prior->source,
                'prior_id' => $prior->id,
                'prior_kid' => $prior->secret_kid,
                'new_id' => $newId,
                'new_kid' => $kid,
                'overlap_hours' => $overlapHours,
            ],
        );

        return redirect()->back()->with([
            'flash' => sprintf(
                'rotated %s HMAC → kid=%s (overlap=%dh)',
                $prior->source,
                $kid,
                $overlapHours,
            ),
            'sender_secret' => $secret,
            'sender_source' => $prior->source,
        ]);
    }
}
