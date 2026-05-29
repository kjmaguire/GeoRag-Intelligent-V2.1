<?php

declare(strict_types=1);

namespace Tests\Feature\Authz;

use App\Support\AuthorizationAuditLogger;
use Illuminate\Log\Events\MessageLogged;
use Illuminate\Support\Facades\Event;
use Tests\TestCase;

/**
 * Module 9 Chunk 9.8 — verify AuthorizationAuditLogger emits a structured
 * payload via the dedicated `authz_audit` log channel. Pulse / Loki ingest
 * from this channel for compliance dashboards.
 *
 * The Log facade dispatches MessageLogged events for every channel write;
 * we listen on the global Event dispatcher to capture them. The `channel`
 * field on the event tells us which channel emitted, so we can filter for
 * `authz_audit` specifically.
 */
final class AuthorizationAuditLoggerTest extends TestCase
{
    /** @var list<MessageLogged> */
    private array $captured = [];

    protected function setUp(): void
    {
        parent::setUp();
        $this->captured = [];
        Event::listen(MessageLogged::class, function (MessageLogged $e): void {
            // Only capture authz_audit channel events.
            if (($e->context['_channel'] ?? null) === 'authz_audit'
                || (isset($e->context['event']) && $e->context['event'] === 'authz.deny')
            ) {
                $this->captured[] = $e;
            }
        });
    }

    public function test_deny_writes_required_keys(): void
    {
        AuthorizationAuditLogger::deny(
            actor: 42,
            targetResource: 'project:0190-uuid',
            reason: 'no_pivot_row',
            targetWorkspaceId: 'a0000000-0000-0000-0000-000000000001',
            context: ['action' => 'show', 'path' => 'api/v1/projects/0190-uuid'],
        );

        $this->assertCount(1, $this->captured, 'one MessageLogged event should fire');
        $event = $this->captured[0];

        $this->assertSame('warning', $event->level);
        $this->assertSame('authz.deny', $event->message);

        $ctx = $event->context;
        $this->assertSame('authz.deny', $ctx['event']);
        $this->assertSame('42', $ctx['actor_user_id']);
        $this->assertSame('project:0190-uuid', $ctx['target_resource']);
        $this->assertSame('no_pivot_row', $ctx['reason']);
        $this->assertSame('a0000000-0000-0000-0000-000000000001', $ctx['target_workspace_id']);
        $this->assertSame('show', $ctx['action']);
        $this->assertArrayHasKey('occurred_at', $ctx);
    }

    public function test_deny_handles_int_actor_id(): void
    {
        AuthorizationAuditLogger::deny(
            actor: 99,
            targetResource: 'collar:abc-123',
            reason: 'cross_workspace',
        );

        $this->assertCount(1, $this->captured);
        $ctx = $this->captured[0]->context;
        $this->assertSame('99', $ctx['actor_user_id']);
        $this->assertSame('collar:abc-123', $ctx['target_resource']);
        $this->assertSame('cross_workspace', $ctx['reason']);
        $this->assertNull($ctx['target_workspace_id']);
    }

    public function test_deny_handles_anonymous_actor(): void
    {
        AuthorizationAuditLogger::deny(
            actor: null,
            targetResource: null,
            reason: 'unauthenticated',
        );

        $this->assertCount(1, $this->captured);
        $ctx = $this->captured[0]->context;
        $this->assertNull($ctx['actor_user_id']);
        $this->assertNull($ctx['target_resource']);
        $this->assertSame('unauthenticated', $ctx['reason']);
    }
}
