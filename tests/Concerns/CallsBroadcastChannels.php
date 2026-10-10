<?php

declare(strict_types=1);

namespace Tests\Concerns;

use App\Models\User;
use Illuminate\Contracts\Broadcasting\Broadcaster as BroadcasterContract;
use ReflectionClass;

/**
 * Call the authorisation closures registered in routes/channels.php directly.
 *
 * Why not POST /broadcasting/auth: phpunit.xml pins BROADCAST_CONNECTION=null,
 * and the null driver's auth endpoint answers 200 whatever the callback
 * returns, so an HTTP-level assertion cannot tell "allowed" from "denied".
 * The closure is the real security gate; this reads it out of the broadcaster
 * the application registered it on and invokes it, which is what
 * QueryChannelAuthorizationTest has always done.
 *
 * Channel patterns are the exact strings passed to Broadcast::channel(), e.g.
 * 'workspace.{workspaceId}.activity'.
 */
trait CallsBroadcastChannels
{
    /**
     * @return array<string, callable> pattern => authorisation closure
     */
    protected function registeredChannels(): array
    {
        $broadcaster = app(BroadcasterContract::class);

        $reflection = new ReflectionClass($broadcaster);
        while ($reflection !== false && ! $reflection->hasProperty('channels')) {
            $reflection = $reflection->getParentClass();
        }
        $this->assertNotFalse($reflection, 'Broadcaster has no `channels` property to inspect.');

        $property = $reflection->getProperty('channels');
        $property->setAccessible(true);

        /** @var array<string, callable> $channels */
        $channels = $property->getValue($broadcaster);

        return $channels;
    }

    /**
     * Run the registered closure for $pattern as $user and return what it returned.
     */
    protected function callChannel(string $pattern, ?User $user, string ...$parameters): mixed
    {
        $channels = $this->registeredChannels();
        $callback = $channels[$pattern] ?? null;
        $this->assertNotNull(
            $callback,
            "Channel '{$pattern}' is not registered in routes/channels.php. Registered: "
            .implode(', ', array_keys($channels)),
        );

        return $callback($user, ...$parameters);
    }
}
