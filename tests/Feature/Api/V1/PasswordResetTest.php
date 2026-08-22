<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\User;
use Illuminate\Auth\Notifications\ResetPassword;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Hash;
use Illuminate\Support\Facades\Notification;
use Tests\TestCase;

class PasswordResetTest extends TestCase
{
    use RefreshDatabase;

    public function test_request_is_enumeration_safe_for_unknown_email(): void
    {
        Notification::fake();

        $response = $this->postJson('/api/v1/auth/forgot-password', [
            'email' => 'missing@example.com',
        ]);

        $response
            ->assertOk()
            ->assertExactJson([
                'message' => 'If an account exists for that email, a password reset link has been sent.',
            ]);
        Notification::assertNothingSent();
    }

    public function test_user_can_request_and_complete_password_reset(): void
    {
        Notification::fake();
        $user = User::factory()->create([
            'email' => 'geologist@example.com',
            'password' => Hash::make('old-password'),
        ]);
        $token = null;

        $this->postJson('/api/v1/auth/forgot-password', [
            'email' => $user->email,
        ])->assertOk();

        Notification::assertSentTo(
            $user,
            ResetPassword::class,
            function (ResetPassword $notification) use (&$token): bool {
                $token = $notification->token;

                return true;
            },
        );

        $this->assertIsString($token);

        $this->postJson('/api/v1/auth/reset-password', [
            'token' => $token,
            'email' => $user->email,
            'password' => 'new-secure-password',
            'password_confirmation' => 'new-secure-password',
        ])->assertOk();

        $this->assertTrue(Hash::check('new-secure-password', $user->refresh()->password));
    }

    public function test_invalid_reset_token_is_rejected(): void
    {
        $user = User::factory()->create();

        $this->postJson('/api/v1/auth/reset-password', [
            'token' => 'invalid-token',
            'email' => $user->email,
            'password' => 'new-secure-password',
            'password_confirmation' => 'new-secure-password',
        ])->assertUnprocessable();
    }

    public function test_reset_says_so_when_no_mailer_is_configured(): void
    {
        // Production runs MAIL_MAILER=log. sendResetLink() then writes the
        // reset URL to the log stream and reports success, so the user was
        // told "a link has been sent" and never received one — and the
        // deliberately non-committal wording meant neither they nor support
        // could tell that from "no such account".
        config(['mail.default' => 'log']);

        $response = $this->postJson('/api/v1/auth/forgot-password', [
            'email' => 'someone@example.com',
        ]);

        $response->assertStatus(503);
        $this->assertStringContainsString(
            'not available',
            (string) $response->json('message'),
        );
    }
}
