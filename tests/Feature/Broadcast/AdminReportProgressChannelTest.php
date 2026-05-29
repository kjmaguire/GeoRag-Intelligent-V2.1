<?php

declare(strict_types=1);

namespace Tests\Feature\Broadcast;

use App\Models\User;
use Illuminate\Contracts\Broadcasting\Broadcaster as BroadcasterContract;
use ReflectionClass;
use Tests\TestCase;

/**
 * Phase H4 §7 — verify the private-admin.reports.{build_id} channel
 * authorisation callback. The cockpit's real-time progress strip is
 * fed from this channel; only admins may subscribe.
 *
 * No DB needed — the callback only reads $user->is_admin and the
 * build_id regex, so we use User::make() to construct an in-memory
 * model without touching the test database.
 */
final class AdminReportProgressChannelTest extends TestCase
{
    private function userWithAdmin(bool $isAdmin): User
    {
        $u = new User;
        $u->is_admin = $isAdmin;

        return $u;
    }

    private function callChannelAuth(User $user, string $buildId): mixed
    {
        $broadcaster = app(BroadcasterContract::class);
        $refl = new ReflectionClass($broadcaster);
        $prop = null;
        while ($refl !== false) {
            if ($refl->hasProperty('channels')) {
                $prop = $refl->getProperty('channels');
                break;
            }
            $refl = $refl->getParentClass();
        }
        $this->assertNotNull($prop, 'Broadcaster has no `channels` property to inspect.');
        $prop->setAccessible(true);
        $channels = $prop->getValue($broadcaster);

        $callback = $channels['admin.reports.{build_id}'] ?? null;
        $this->assertNotNull($callback, 'admin.reports.{build_id} channel not registered.');

        return $callback($user, $buildId);
    }

    public function test_admin_with_valid_build_id_is_authorised(): void
    {
        $admin = $this->userWithAdmin(true);

        $ok = $this->callChannelAuth($admin, '550e8400-e29b-41d4-a716-446655440000');

        $this->assertTrue((bool) $ok);
    }

    public function test_non_admin_is_rejected(): void
    {
        $user = $this->userWithAdmin(false);

        $this->assertFalse($this->callChannelAuth($user, '550e8400-e29b-41d4-a716-446655440000'));
    }

    public function test_invalid_uuid_shape_is_rejected(): void
    {
        $admin = $this->userWithAdmin(true);

        // Wrong shape — bare word
        $this->assertFalse($this->callChannelAuth($admin, 'not-a-uuid'));
        // Wrong shape — missing dashes
        $this->assertFalse($this->callChannelAuth($admin, '550e8400e29b41d4a716446655440000'));
        // Wrong shape — too short
        $this->assertFalse($this->callChannelAuth($admin, '550e8400-e29b-41d4-a716'));
    }
}
