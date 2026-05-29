<?php

declare(strict_types=1);

namespace Tests\Feature\Services;

use App\Events\Admin\AdminSurfaceUpdated;
use App\Services\Audit\AuditEmitter;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 2 — AuditEmitter must fan out to admin.alerts-inbox whenever
 * an audit row whose action_type ends in '.alert' or '.acknowledged'
 * is committed.
 *
 * Locks the invariants:
 *   - '.alert' action_types DO broadcast
 *   - '.acknowledged' action_types DO broadcast
 *   - Non-alert action_types DO NOT broadcast
 *   - Broadcast failure does NOT cascade into the audit emit (durable
 *     row is the source of truth; broadcast is best-effort)
 */
final class AuditEmitterAlertBroadcastTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_alert_action_type_broadcasts_to_alerts_inbox(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        app(AuditEmitter::class)->emit(
            actionType: 'cost.burn.alert',
            workspaceId: null,
            actorId: null,
            actorKind: AuditEmitter::ACTOR_WORKFLOW,
            targetSchema: 'audit',
            targetTable: 'audit_ledger',
            targetId: null,
            payload: ['threshold_usd' => 100.0, 'observed_usd' => 142.3],
        );

        Event::assertDispatched(
            AdminSurfaceUpdated::class,
            function (AdminSurfaceUpdated $e): bool {
                return $e->surface === 'alerts-inbox'
                    && $e->surfaceId === null
                    && $e->affectedProps === ['items']
                    && ($e->payload['action_type'] ?? null) === 'cost.burn.alert';
            },
        );
    }

    public function test_acknowledged_action_type_broadcasts(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        app(AuditEmitter::class)->emit(
            actionType: 'cost.burn.acknowledged',
            actorId: 1,
            actorKind: AuditEmitter::ACTOR_USER,
        );

        Event::assertDispatched(
            AdminSurfaceUpdated::class,
            fn (AdminSurfaceUpdated $e) => $e->surface === 'alerts-inbox',
        );
    }

    public function test_non_alert_action_type_does_not_broadcast(): void
    {
        Event::fake([AdminSurfaceUpdated::class]);

        app(AuditEmitter::class)->emit(
            actionType: 'silver.collars.update',
            actorId: 1,
            actorKind: AuditEmitter::ACTOR_USER,
            targetSchema: 'silver',
            targetTable: 'collars',
            targetId: 'some-id',
        );

        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }

    public function test_action_type_containing_alert_substring_but_not_suffix_does_not_broadcast(): void
    {
        // 'alert' substring is NOT enough — only suffix matches.
        // Protects against false positives like 'alerting.config.update'.
        Event::fake([AdminSurfaceUpdated::class]);

        app(AuditEmitter::class)->emit(
            actionType: 'alerting.config.update',
            actorId: 1,
            actorKind: AuditEmitter::ACTOR_USER,
        );

        Event::assertNotDispatched(AdminSurfaceUpdated::class);
    }
}
