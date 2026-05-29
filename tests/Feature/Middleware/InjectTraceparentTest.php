<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use App\Http\Middleware\InjectTraceparent;
use Illuminate\Support\Facades\Route;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.6 — verify the W3C Trace Context middleware accepts
 * valid inbound headers, mints new ones when missing/malformed, and re-emits
 * on the response. Mirror of `src/fastapi/tests/test_traceparent.py`.
 */
final class InjectTraceparentTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        Route::get('/_test/trace/echo', function (\Illuminate\Http\Request $r) {
            return [
                'traceparent' => $r->attributes->get(InjectTraceparent::ATTRIBUTE_KEY),
                'trace_id' => InjectTraceparent::traceIdOf(
                    $r->attributes->get(InjectTraceparent::ATTRIBUTE_KEY),
                ),
            ];
        });
    }

    public function test_valid_inbound_traceparent_is_forwarded(): void
    {
        $incoming = '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01';
        $resp = $this->withHeaders(['traceparent' => $incoming])->get('/_test/trace/echo');

        $resp->assertOk();
        $resp->assertHeader('traceparent', $incoming);
        $this->assertSame($incoming, $resp->json('traceparent'));
        $this->assertSame('0123456789abcdef0123456789abcdef', $resp->json('trace_id'));
    }

    public function test_missing_traceparent_is_minted(): void
    {
        $resp = $this->get('/_test/trace/echo');

        $resp->assertOk();
        $minted = $resp->headers->get('traceparent');
        $this->assertNotNull($minted);
        $this->assertTrue(InjectTraceparent::isValid($minted));
        $this->assertSame(substr($minted, 3, 32), $resp->json('trace_id'));
    }

    public function test_malformed_traceparent_is_replaced(): void
    {
        $resp = $this->withHeaders(['traceparent' => 'not-valid'])->get('/_test/trace/echo');

        $minted = $resp->headers->get('traceparent');
        $this->assertTrue(InjectTraceparent::isValid($minted));
        $this->assertNotSame('not-valid', $minted);
    }

    public function test_validator_rejects_wrong_version(): void
    {
        $this->assertFalse(InjectTraceparent::isValid(
            '01-0123456789abcdef0123456789abcdef-0123456789abcdef-01',
        ));
    }

    public function test_validator_rejects_uppercase_hex(): void
    {
        $this->assertFalse(InjectTraceparent::isValid(
            '00-0123456789ABCDEF0123456789ABCDEF-0123456789ABCDEF-01',
        ));
    }

    public function test_unsampled_flag_is_preserved(): void
    {
        $unsampled = '00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-00';
        $resp = $this->withHeaders(['traceparent' => $unsampled])->get('/_test/trace/echo');

        $resp->assertHeader('traceparent', $unsampled);
    }

    public function test_minted_traceparents_are_unique(): void
    {
        $set = [];
        for ($i = 0; $i < 50; $i++) {
            $set[InjectTraceparent::mint()] = true;
        }
        $this->assertCount(50, $set);
    }
}
