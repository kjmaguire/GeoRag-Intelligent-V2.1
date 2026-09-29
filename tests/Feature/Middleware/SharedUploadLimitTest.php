<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use App\Support\Uploads;
use Inertia\Testing\AssertableInertia;
use Tests\TestCase;

/**
 * FE-2: the upload screens read the server's one upload ceiling from the
 * `upload_limit` shared prop instead of hard-coding their own (NewProject
 * said 6 GB against a 512 MB server cap).
 */
final class SharedUploadLimitTest extends TestCase
{
    public function test_every_inertia_page_shares_the_upload_ceiling(): void
    {
        $this->get('/login')
            ->assertStatus(200)
            ->assertInertia(fn (AssertableInertia $page) => $page
                ->where('upload_limit.bytes', Uploads::maxBytes())
                ->where('upload_limit.human', Uploads::maxHuman()));
    }

    public function test_it_follows_the_environment_override(): void
    {
        putenv('GEORAG_MAX_UPLOAD_BYTES='.(256 * 1024 * 1024));

        try {
            $this->get('/login')
                ->assertInertia(fn (AssertableInertia $page) => $page
                    ->where('upload_limit.bytes', 256 * 1024 * 1024)
                    ->where('upload_limit.human', '256 MB'));
        } finally {
            putenv('GEORAG_MAX_UPLOAD_BYTES');
        }
    }
}
