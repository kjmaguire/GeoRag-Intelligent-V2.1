<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use App\Services\Audit\AuditEmitter;
use App\Services\DecisionIntelligence\RecordDecision;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 2 Step 6 — `/admin/integrations` Kestra dashboard.
 *
 * Joins three sides into one operator surface:
 *
 *   1. Hatchet-side workflow registry — the canonical names + IO contracts
 *      live in code (FastAPI ``app.routers.integrations_trigger.FLOW_REGISTRY``).
 *      The PHP side hard-codes the same list to avoid an inter-service
 *      RPC just for a UI listing. Phase 3+ moves this to a shared
 *      registry table.
 *   2. Feature-flag state from ``workspace.feature_flags`` — one
 *      ``activepieces.<flow>.enabled`` row per flow.
 *   3. Kestra-side flow metadata from the ``activepieces`` logical
 *      DB (``flow`` + ``flow_version``) — surfaces what the operator
 *      has actually wired up in Kestra' UI.
 *   4. Last 24h Hatchet run rollup from the ``hatchet`` logical DB
 *      (``v1_runs_olap``).
 *
 * Auth: 'admin' Gate.
 *
 * Routes:
 *   GET   /admin/integrations                          index
 *   PATCH /admin/integrations/flags/{flag_name}        toggleFlag
 */
class IntegrationsController extends Controller
{
    /**
     * Phase 4 Step 4 — flow catalog comes from workflow.flow_registry
     * (DB-driven). Cached at the Laravel layer for 60s to match the
     * FastAPI loader's cache TTL. Adding a flow is `INSERT INTO …`,
     * not a code deploy.
     */
    private const REGISTRY_CACHE_KEY = 'phase4.flow_registry';

    private const REGISTRY_CACHE_SECONDS = 60;

    /**
     * @return list<array{flow_name: string, flag_name: ?string, kind: string, description: string}>
     */
    private function registeredFlows(): array
    {
        return cache()->remember(
            self::REGISTRY_CACHE_KEY,
            self::REGISTRY_CACHE_SECONDS,
            static function (): array {
                $rows = DB::connection('pgsql')
                    ->table('workflow.flow_registry')
                    ->where('enabled', true)
                    // Phase 4 deliberately hides the placeholder phase2_smoke
                    // workflow from the operator dashboard — it stays in the
                    // registry as a connectivity debug surface but doesn't
                    // pollute /admin/integrations.
                    ->where('kind', '!=', 'placeholder')
                    ->orderBy('kind')
                    ->orderBy('flow_name')
                    ->get(['flow_name', 'flag_name', 'kind', 'description']);

                return $rows->map(static fn (object $r) => [
                    'flow_name' => (string) $r->flow_name,
                    'flag_name' => $r->flag_name !== null ? (string) $r->flag_name : null,
                    'kind' => (string) $r->kind,
                    'description' => (string) $r->description,
                ])->all();
            },
        );
    }

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $flagsByName = $this->loadFlags();
        $hatchetRollupsByName = $this->loadHatchetRunRollups();
        $auditCountsByAction = $this->loadAuditCounts24h();

        $flows = [];
        foreach ($this->registeredFlows() as $flow) {
            $flagRow = $flagsByName[$flow['flag_name']] ?? null;
            $rollup = $hatchetRollupsByName[$flow['flow_name']] ?? null;

            $flows[] = [
                'flow_name' => $flow['flow_name'],
                'flag_name' => $flow['flag_name'],
                'kind' => $flow['kind'],
                'description' => $flow['description'],
                'enabled' => $flagRow['enabled'] ?? false,
                'flag_updated_at' => $flagRow['updated_at'] ?? null,
                'last_24h' => $rollup ?? [
                    'completed' => 0, 'failed' => 0, 'running' => 0,
                    'queued' => 0, 'cancelled' => 0,
                    'p50_duration_ms' => null, 'p95_duration_ms' => null,
                    'last_started_at' => null,
                ],
                'audit_emissions_24h' => $this->matchAuditCount($auditCountsByAction, $flow['flow_name']),
            ];
        }

        // Phase 10 Step 3 — surface the one-shot sender_secret flash
        // so the Inertia page can render the secret-copy banner. The
        // session flash bag holds the value only for the next page
        // load; this read pulls it out for the prop AND clears it.
        // Guard against requests built without a session (probe
        // scripts, tests) — hasSession()==false means there's
        // nothing to pull.
        $senderSecret = $request->hasSession()
            ? $request->session()->pull('sender_secret')
            : null;
        $senderSource = $request->hasSession()
            ? $request->session()->pull('sender_source')
            : null;

        return Inertia::render('Admin/Integrations', [
            'flows' => $flows,
            'kestra_flows' => $this->loadKestraFlows(),
            'flow_history' => $this->loadFlagHistory(),
            'senders' => $this->loadSenders(),
            'flow_jwt_keys' => $this->loadFlowJwtKeys(),
            'rotation_history' => $this->loadRotationHistory(),
            'new_sender_secret' => $senderSecret,
            'new_sender_source' => $senderSource,
        ]);
    }

    /**
     * Phase 4 Step 5 — toggle a sender's disabled state.
     *
     * PATCH /admin/integrations/senders/{id}/{action}
     *   action ∈ {disable, enable}
     */
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

        return redirect()->route('admin.integrations')->with(
            'flash', sprintf('sender %s %sd', substr($id, 0, 8), $action),
        );
    }

    /**
     * Phase 9 Step 2 (R-P8-1) — operator-facing rotate-with-overlap
     * for per-flow JWT signing keys.
     *
     * POST /admin/integrations/jwt-keys/rotate
     *   flow_name      ∈ workflow.flow_registry (validated)
     *   overlap_hours  ∈ [0, 168]  (0 = immediate retire, 168 = 7d)
     *
     * Generates a fresh kid (`rotated-{unix}`) + a 32-byte random
     * secret, then calls `workflow.set_flow_jwt_secret(...)` with
     * the requested overlap window. The prior kid (if any) gets
     * `valid_until = now() + overlap_hours`.
     */
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

        return redirect()->route('admin.integrations')->with(
            'flash',
            sprintf(
                'rotated %s → kid=%s (overlap=%dh)',
                $flowName,
                $kid,
                $overlapHours,
            ),
        );
    }

    /**
     * Phase 10 Step 3 — register a new external notification sender
     * from the admin UI. Wraps `usage.register_external_notification_sender(...)`
     * with the same encryption-key GUC pattern used by the rotate
     * action.
     *
     * POST /admin/integrations/senders
     *   source       — sender identifier (lowercase + underscores)
     *   description  — optional free-form note
     *
     * Generates a fresh HMAC secret (32 random bytes, hex-encoded),
     * registers the sender under kid='primary', and surfaces the
     * secret in the flash bag for one-time operator copy. The secret
     * itself is NEVER written to the audit ledger; only the
     * registration event (source + kid + actor) is.
     */
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
            return redirect()->route('admin.integrations')->with(
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
        return redirect()->route('admin.integrations')->with([
            'flash' => sprintf('registered %s (kid=%s)', $source, $kid),
            'sender_secret' => $secret,
            'sender_source' => $source,
        ]);
    }

    /**
     * Phase 12 Step 4 (R-P10-1) — rotate a sender's HMAC secret.
     *
     * POST /admin/integrations/senders/{id}/rotate-hmac
     *
     * The Phase 4 Step 1 registry stores one row per (source, kid).
     * Rotation creates a NEW row via
     * `usage.register_external_notification_sender(..., p_supersedes=$id)`
     * and disables the prior row, mirroring the Phase 9 Step 2 JWT
     * rotate semantics on the senders side.
     *
     * The freshly-generated HMAC secret is flashed for one-time
     * operator copy via the same banner the Phase 10 Step 3 register
     * path uses.
     */
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

        return redirect()->route('admin.integrations')->with([
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

    /**
     * Phase 12 Step 4 (R-P10-2) — recent rotation events surfaced for
     * the UI panel. Joins both rotation actions
     * (`workflow.jwt_key.rotated` and
     * `usage.external_notification_sender.hmac_rotated`) into a
     * single time-ordered list, capped at 25 most recent.
     *
     * @return array<int, array<string, mixed>>
     */
    private function loadRotationHistory(): array
    {
        try {
            $rows = DB::connection('pgsql')->select(
                <<<'SQL'
                SELECT action_type,
                       created_at,
                       actor_id,
                       payload->>'flow_name'   AS flow_name,
                       payload->>'source'      AS source,
                       payload->>'prior_kid'   AS prior_kid,
                       payload->>'new_kid'     AS new_kid,
                       payload->>'overlap_hours' AS overlap_hours
                  FROM audit.audit_ledger
                 WHERE action_type IN (
                       'workflow.jwt_key.rotated',
                       'usage.external_notification_sender.hmac_rotated'
                   )
                 ORDER BY created_at DESC
                 LIMIT 25
                SQL,
            );
        } catch (\Throwable $e) {
            return [];
        }

        return array_map(static fn (object $r) => [
            'action_type' => (string) $r->action_type,
            'created_at' => (string) ($r->created_at ?? ''),
            'actor_id' => $r->actor_id !== null ? (int) $r->actor_id : null,
            'flow_name' => $r->flow_name !== null ? (string) $r->flow_name : null,
            'source' => $r->source !== null ? (string) $r->source : null,
            'prior_kid' => $r->prior_kid !== null ? (string) $r->prior_kid : null,
            'new_kid' => $r->new_kid !== null ? (string) $r->new_kid : null,
            'overlap_hours' => $r->overlap_hours !== null ? (int) $r->overlap_hours : null,
        ], $rows);
    }

    public function toggleFlag(Request $request, string $flagName): RedirectResponse
    {
        $this->authorize('admin');

        $known = array_column($this->registeredFlows(), 'flag_name');
        if (! in_array($flagName, $known, true)) {
            abort(404, "unknown flag: $flagName");
        }

        $validated = $request->validate([
            'value' => ['required', 'boolean'],
            'reason' => ['sometimes', 'nullable', 'string', 'max:500'],
        ]);
        $value = (bool) $validated['value'];
        $reason = $validated['reason'] ?? null;
        $userId = Auth::id();

        DB::connection('pgsql')->statement(
            'INSERT INTO workspace.feature_flags
                (workspace_id, flag_name, bool_value, updated_by, updated_at)
             VALUES (NULL, ?, ?, ?, now())
             ON CONFLICT (workspace_id, flag_name) DO UPDATE
                SET bool_value = EXCLUDED.bool_value,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()',
            [$flagName, $value, $userId],
        );

        // Doc-phase 133 — §21.3 workflow_enablement capture hook.
        // Records the toggle as a Decision Intelligence record so it
        // shows up on /admin/decision-history. Best-effort: failures
        // don't block the flag flip (the flip itself is already
        // persisted + audit-anchored by the trigger on the row above).
        if ($userId !== null) {
            try {
                app(RecordDecision::class)->record(
                    workspaceId: RecordDecision::PLATFORM_OPS_WORKSPACE_ID,
                    decisionType: 'workflow_enablement',
                    recommendation: sprintf(
                        '%s feature flag %s',
                        $value ? 'Enable' : 'Disable',
                        $flagName,
                    ),
                    humanDecision: 'accepted',
                    decidedByUserId: $userId,
                    reason: $reason,
                    optionsConsidered: [
                        [
                            'label' => $value ? 'enable' : 'disable',
                            'description' => $value
                                ? "Enable {$flagName}"
                                : "Disable {$flagName}",
                            'was_chosen' => true,
                        ],
                        [
                            'label' => $value ? 'disable' : 'enable',
                            'description' => $value
                                ? "Disable {$flagName}"
                                : "Enable {$flagName}",
                            'was_chosen' => false,
                        ],
                    ],
                );
            } catch (\Throwable $e) {
                // Log but don't fail the flag toggle. The toggle is
                // the authoritative event; the decision record is a
                // §9.12 enrichment.
                report($e);
            }
        }

        return redirect()->route('admin.integrations')->with(
            'flash',
            sprintf('%s = %s', $flagName, $value ? 'true' : 'false'),
        );
    }

    /**
     * @return array<string, array{enabled: bool, updated_at: ?string}>
     */
    private function loadFlags(): array
    {
        $names = array_column($this->registeredFlows(), 'flag_name');
        if ($names === []) {
            return [];
        }
        $rows = DB::connection('pgsql')
            ->table('workspace.feature_flags')
            ->whereNull('workspace_id')
            ->whereIn('flag_name', $names)
            ->get(['flag_name', 'bool_value', 'updated_at']);

        $by = [];
        foreach ($rows as $r) {
            $by[$r->flag_name] = [
                'enabled' => (bool) $r->bool_value,
                'updated_at' => $r->updated_at,
            ];
        }

        return $by;
    }

    /**
     * @return array<string, array<string, mixed>>
     */
    private function loadHatchetRunRollups(): array
    {
        $names = array_column($this->registeredFlows(), 'flow_name');
        if ($names === []) {
            return [];
        }
        $placeholders = implode(',', array_fill(0, count($names), '?'));
        $rows = DB::connection('pgsql_hatchet')->select(
            <<<SQL
            SELECT w.name AS workflow_name,
                   count(*) FILTER (WHERE runs.readable_status = 'COMPLETED')         AS completed,
                   count(*) FILTER (WHERE runs.readable_status = 'FAILED')            AS failed,
                   count(*) FILTER (WHERE runs.readable_status = 'RUNNING')           AS running,
                   count(*) FILTER (WHERE runs.readable_status = 'QUEUED')            AS queued,
                   count(*) FILTER (WHERE runs.readable_status IN ('CANCELLED','EVICTED')) AS cancelled,
                   max(runs.inserted_at)                                              AS last_started_at
            FROM v1_runs_olap runs
            JOIN "WorkflowVersion" v ON v.id = runs.workflow_version_id
            JOIN "Workflow"        w ON w.id = v."workflowId"
            WHERE runs.inserted_at > now() - interval '24 hours'
              AND w.name IN ($placeholders)
            GROUP BY w.name
            SQL,
            $names,
        );

        // R-P2-6 — durations come from v1_task_events_olap by pairing
        // STARTED + FINISHED events per task. Tasks live below runs in
        // Hatchet V1; we compute task-level durations and aggregate to
        // the workflow level with percentile_disc.
        $durations = $this->loadHatchetDurations($names, $placeholders);

        $by = [];
        foreach ($rows as $r) {
            $d = $durations[$r->workflow_name] ?? ['p50' => null, 'p95' => null];
            $by[$r->workflow_name] = [
                'completed' => (int) $r->completed,
                'failed' => (int) $r->failed,
                'running' => (int) $r->running,
                'queued' => (int) $r->queued,
                'cancelled' => (int) $r->cancelled,
                'p50_duration_ms' => $d['p50'] !== null ? (int) $d['p50'] : null,
                'p95_duration_ms' => $d['p95'] !== null ? (int) $d['p95'] : null,
                'last_started_at' => $r->last_started_at,
            ];
        }

        return $by;
    }

    /**
     * @param list<string> $names
     *
     * @return array<string, array{p50: ?float, p95: ?float}>
     */
    private function loadHatchetDurations(array $names, string $placeholders): array
    {
        $rows = DB::connection('pgsql_hatchet')->select(
            <<<SQL
            WITH task_pairs AS (
                SELECT e.task_id,
                       e.task_inserted_at,
                       e.workflow_id,
                       max(e.event_timestamp) FILTER (WHERE e.event_type = 'FINISHED') AS finished_at,
                       min(e.event_timestamp) FILTER (WHERE e.event_type = 'STARTED')  AS started_at
                FROM v1_task_events_olap e
                WHERE e.event_type IN ('FINISHED','STARTED')
                  AND e.inserted_at > now() - interval '24 hours'
                GROUP BY e.task_id, e.task_inserted_at, e.workflow_id
                HAVING max(e.event_timestamp) FILTER (WHERE e.event_type = 'FINISHED') IS NOT NULL
                   AND min(e.event_timestamp) FILTER (WHERE e.event_type = 'STARTED')  IS NOT NULL
            )
            SELECT w.name AS workflow_name,
                   percentile_disc(0.50) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (tp.finished_at - tp.started_at)) * 1000) AS p50,
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (tp.finished_at - tp.started_at)) * 1000) AS p95
            FROM task_pairs tp
            JOIN "Workflow" w ON w.id = tp.workflow_id
            WHERE w.name IN ($placeholders)
            GROUP BY w.name
            SQL,
            $names,
        );

        $by = [];
        foreach ($rows as $r) {
            $by[$r->workflow_name] = [
                'p50' => $r->p50,
                'p95' => $r->p95,
            ];
        }

        return $by;
    }

    /**
     * @return array<int, array{action_type: string, n: int}>
     */
    private function loadAuditCounts24h(): array
    {
        $actions = [
            'public_geo.pull.complete',
            'external_notification.received',
        ];
        $rows = DB::connection('pgsql')
            ->table('audit.audit_ledger')
            ->select('action_type', DB::raw('count(*) AS n'))
            ->whereIn('action_type', $actions)
            ->where('created_at', '>=', now()->subDay())
            ->groupBy('action_type')
            ->get();

        $by = [];
        foreach ($rows as $r) {
            $by[$r->action_type] = (int) $r->n;
        }

        return [
            'public_geoscience_pull' => $by['public_geo.pull.complete'] ?? 0,
            'external_notification' => $by['external_notification.received'] ?? 0,
        ];
    }

    private function matchAuditCount(array $byFlow, string $flowName): int
    {
        return (int) ($byFlow[$flowName] ?? 0);
    }

    /**
     * Phase 3 Step 6 — Kestra-side flow listing. Reads `flows` from the
     * kestra logical DB. Schema (Kestra v1.x):
     *   - id        text  (primary key, the YAML `id` field)
     *   - namespace text
     *   - revision  int
     *   - source_code text  (the YAML body — large; we only return id+ns)
     *   - created   timestamp
     *   - updated   timestamp
     *
     * The Kestra DB stores flow definitions under `value` JSONB, but
     * filtering is easiest via the indexed `id` + `namespace` columns.
     *
     * @return array<int, array<string, mixed>>
     */
    private function loadKestraFlows(): array
    {
        try {
            // Kestra v1.x: id/namespace/revision are GENERATED columns from
            // `value` JSONB; created/updated live inside `value` only.
            $rows = DB::connection('pgsql_kestra')->select(
                <<<'SQL'
                SELECT id,
                       namespace,
                       revision,
                       value->>'created' AS created,
                       value->>'updated' AS updated
                  FROM flows
                 WHERE NOT deleted
                 ORDER BY namespace, id
                 LIMIT 50
                SQL
            );
        } catch (\Throwable $e) {
            // Kestra DB unreachable → empty list.
            return [];
        }

        return array_map(static fn (object $r) => [
            'id' => (string) ($r->id ?? ''),
            'namespace' => (string) ($r->namespace ?? ''),
            'revision' => $r->revision !== null ? (int) $r->revision : null,
            'created' => (string) ($r->created ?? ''),
            'updated' => (string) ($r->updated ?? ''),
        ], $rows);
    }

    /**
     * Phase 4 Step 5 — per-sender HMAC registry rows + last 24h receive
     * counts. Joined to audit_ledger so the operator can see traffic per
     * sender without leaving the dashboard.
     *
     * @return array<int, array<string, mixed>>
     */
    private function loadSenders(): array
    {
        try {
            $rows = DB::connection('pgsql')->select(
                <<<'SQL'
                WITH counts_24h AS (
                    SELECT payload->>'source' AS source,
                           count(*) AS n
                      FROM audit.audit_ledger
                     WHERE action_type = 'external_notification.received'
                       AND created_at >= now() - interval '24 hours'
                     GROUP BY 1
                )
                SELECT s.id::text         AS id,
                       s.source,
                       s.secret_kid,
                       s.created_at,
                       s.last_seen_at,
                       s.disabled_at,
                       COALESCE(c.n, 0)::int AS receive_count_24h
                  FROM usage.external_notification_senders s
                  LEFT JOIN counts_24h c ON c.source = s.source
                 ORDER BY s.source, s.created_at DESC
                SQL
            );
        } catch (\Throwable $e) {
            return [];
        }

        return array_map(static fn (object $r) => [
            'id' => (string) $r->id,
            'source' => (string) $r->source,
            'secret_kid' => (string) $r->secret_kid,
            'created_at' => (string) ($r->created_at ?? ''),
            'last_seen_at' => $r->last_seen_at !== null ? (string) $r->last_seen_at : null,
            'disabled_at' => $r->disabled_at !== null ? (string) $r->disabled_at : null,
            'receive_count_24h' => (int) $r->receive_count_24h,
        ], $rows);
    }

    /**
     * Phase 8 Step 2 (R-P7-2) — per-flow JWT key history for the
     * Inertia "JWT keys" panel. Returns one row per currently-active
     * kid (valid_until is NULL or in the future). Phase 6 Step 3
     * introduced overlap-window rotation, so a flow may have 2+
     * active kids during the overlap.
     *
     * @return array<int, array<string, mixed>>
     */
    private function loadFlowJwtKeys(): array
    {
        try {
            $rows = DB::connection('pgsql')->select(
                <<<'SQL'
                SELECT k.flow_name,
                       k.kid,
                       k.valid_from,
                       k.valid_until,
                       (k.valid_until IS NULL OR k.valid_until > clock_timestamp())
                         AS is_active,
                       k.created_at
                  FROM workflow.flow_jwt_keys k
                 WHERE k.valid_from <= clock_timestamp()
                 ORDER BY k.flow_name, k.valid_from DESC
                SQL
            );
        } catch (\Throwable $e) {
            return [];
        }

        return array_map(static fn (object $r) => [
            'flow_name' => (string) $r->flow_name,
            'kid' => (string) $r->kid,
            'valid_from' => (string) ($r->valid_from ?? ''),
            'valid_until' => $r->valid_until !== null ? (string) $r->valid_until : null,
            'is_active' => (bool) $r->is_active,
            'created_at' => (string) ($r->created_at ?? ''),
        ], $rows);
    }

    /**
     * Recent flag flips (R-P1-6 sidecar). Helps the operator see who
     * bumped what, when.
     *
     * @return array<int, array<string, mixed>>
     */
    private function loadFlagHistory(): array
    {
        $names = array_column($this->registeredFlows(), 'flag_name');
        if ($names === []) {
            return [];
        }
        $rows = DB::connection('pgsql')
            ->table('workspace.feature_flag_history')
            ->whereIn('flag_name', $names)
            ->orderByDesc('changed_at')
            ->limit(50)
            ->get(['op', 'flag_name', 'old_bool_value', 'new_bool_value', 'actor_id', 'changed_at']);

        return $rows->map(static fn (object $r) => [
            'op' => $r->op,
            'flag_name' => $r->flag_name,
            'old_value' => $r->old_bool_value,
            'new_value' => $r->new_bool_value,
            'actor_id' => $r->actor_id !== null ? (int) $r->actor_id : null,
            'changed_at' => $r->changed_at,
        ])->all();
    }
}
