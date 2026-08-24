/**
 * Ref-counted Echo subscriptions.
 *
 * The fake Echo below reproduces the two properties of the real connector
 * that combine into the bug: `private(name)` returns the SAME channel
 * object for a given name, and `leave(name)` destroys it for everyone.
 * (See laravel-echo's PusherConnector — `privateChannel` memoises into
 * `this.channels`, `leaveChannel` calls `unsubscribe()` and deletes.)
 * A fake that hands out a fresh channel per call would pass every one of
 * these tests while the real thing kept dropping listeners.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import {
    listenPrivate,
    subscriberCount,
    __resetEchoChannelRefCounts,
} from '../echoChannel';

type Handler = (payload: unknown) => void;

class FakeChannel {
    handlers = new Map<string, Set<Handler>>();

    /** Set false by the connector when the channel is left. */
    subscribed = true;

    listen(event: string, handler: Handler): this {
        const set = this.handlers.get(event) ?? new Set<Handler>();
        set.add(handler);
        this.handlers.set(event, set);
        return this;
    }

    stopListening(event: string, handler?: Handler): this {
        const set = this.handlers.get(event);
        if (!set) return this;
        if (handler) set.delete(handler);
        else set.clear();
        return this;
    }

    emit(event: string, payload: unknown): void {
        if (!this.subscribed) return;
        for (const handler of this.handlers.get(event) ?? []) handler(payload);
    }

    handlerCount(event: string): number {
        return this.handlers.get(event)?.size ?? 0;
    }
}

class FakeEcho {
    channels = new Map<string, FakeChannel>();

    /** Names passed to leave(), in order — asserts the "last one out" rule. */
    left: string[] = [];

    private(name: string): FakeChannel {
        let channel = this.channels.get(name);
        if (!channel) {
            channel = new FakeChannel();
            this.channels.set(name, channel);
        }
        return channel;
    }

    leave(name: string): void {
        this.left.push(name);
        const channel = this.channels.get(name);
        if (channel) {
            // What the real connector does: unsubscribe, then forget. Any
            // component still holding this object is now deaf.
            channel.subscribed = false;
            channel.handlers.clear();
            this.channels.delete(name);
        }
    }
}

const CHANNEL = 'project.11111111-1111-1111-1111-111111111111.ingestion';
const EVENT = '.ingestion.progress';

let echo: FakeEcho;

beforeEach(() => {
    __resetEchoChannelRefCounts();
    echo = new FakeEcho();
    (window as unknown as { Echo?: unknown }).Echo = echo;
});

afterEach(() => {
    delete (window as unknown as { Echo?: unknown }).Echo;
    vi.restoreAllMocks();
});

describe('listenPrivate', () => {
    it('delivers events to the handler', () => {
        const handler = vi.fn();
        listenPrivate(CHANNEL, EVENT, handler);

        echo.private(CHANNEL).emit(EVENT, { status: 'completed' });

        expect(handler).toHaveBeenCalledWith({ status: 'completed' });
    });

    it('is a no-op without window.Echo, and its unsubscribe is safe to call', () => {
        delete (window as unknown as { Echo?: unknown }).Echo;

        const unsubscribe = listenPrivate(CHANNEL, EVENT, vi.fn());

        expect(subscriberCount(CHANNEL)).toBe(0);
        expect(() => unsubscribe()).not.toThrow();
    });

    it('counts every subscriber on the same channel', () => {
        listenPrivate(CHANNEL, EVENT, vi.fn());
        listenPrivate(CHANNEL, '.workspace.data_updated', vi.fn());
        listenPrivate(CHANNEL, EVENT, vi.fn());

        expect(subscriberCount(CHANNEL)).toBe(3);
    });
});

describe('the shared-channel teardown this replaces', () => {
    it('keeps a sibling listening when one subscriber unmounts', () => {
        const shellToast = vi.fn();
        const pageReload = vi.fn();

        listenPrivate(CHANNEL, EVENT, shellToast);
        const leavePage = listenPrivate(CHANNEL, EVENT, pageReload);

        // The page navigates away; the shell persists.
        leavePage();
        echo.private(CHANNEL).emit(EVENT, { status: 'completed' });

        expect(shellToast).toHaveBeenCalledTimes(1);
        expect(pageReload).not.toHaveBeenCalled();
        expect(echo.left).toEqual([]);
    });

    it('unbinds only the departing handler, not the whole channel', () => {
        const stay = vi.fn();
        const go = vi.fn();

        listenPrivate(CHANNEL, EVENT, stay);
        listenPrivate(CHANNEL, EVENT, go)();

        expect(echo.private(CHANNEL).handlerCount(EVENT)).toBe(1);
        expect(subscriberCount(CHANNEL)).toBe(1);
    });

    it('leaves the channel once the last subscriber is gone', () => {
        const first = listenPrivate(CHANNEL, EVENT, vi.fn());
        const second = listenPrivate(CHANNEL, EVENT, vi.fn());

        first();
        expect(echo.left).toEqual([]);

        second();
        expect(echo.left).toEqual([CHANNEL]);
        expect(subscriberCount(CHANNEL)).toBe(0);
    });

    it('does not leak the subscription across a full mount/unmount cycle', () => {
        listenPrivate(CHANNEL, EVENT, vi.fn())();
        listenPrivate(CHANNEL, EVENT, vi.fn())();

        expect(echo.left).toEqual([CHANNEL, CHANNEL]);
        expect(subscriberCount(CHANNEL)).toBe(0);
    });
});

describe('unsubscribe is idempotent', () => {
    it('a double-invoked cleanup cannot strand a live listener', () => {
        const survivor = vi.fn();
        listenPrivate(CHANNEL, EVENT, survivor);
        const unsubscribe = listenPrivate(CHANNEL, EVENT, vi.fn());

        // React StrictMode runs cleanups twice in development.
        unsubscribe();
        unsubscribe();

        expect(subscriberCount(CHANNEL)).toBe(1);
        expect(echo.left).toEqual([]);

        echo.private(CHANNEL).emit(EVENT, { status: 'partial' });
        expect(survivor).toHaveBeenCalledTimes(1);
    });
});

describe('channels are counted independently', () => {
    it('leaving one project channel does not touch another', () => {
        const other = 'project.22222222-2222-2222-2222-222222222222.ingestion';
        const otherHandler = vi.fn();

        listenPrivate(CHANNEL, EVENT, vi.fn())();
        listenPrivate(other, EVENT, otherHandler);

        expect(echo.left).toEqual([CHANNEL]);

        echo.private(other).emit(EVENT, { status: 'completed' });
        expect(otherHandler).toHaveBeenCalledTimes(1);
    });
});
