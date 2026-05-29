<?php

declare(strict_types=1);

namespace App\Providers;

use Illuminate\Support\Facades\Gate;
use Laravel\Horizon\Horizon;
use Laravel\Horizon\HorizonApplicationServiceProvider;

class HorizonServiceProvider extends HorizonApplicationServiceProvider
{
    /**
     * Bootstrap any application services.
     */
    public function boot(): void
    {
        parent::boot();

        // Horizon::routeSmsNotificationsTo('15556667777');
        // Horizon::routeMailNotificationsTo('example@example.com');
        // Horizon::routeSlackNotificationsTo('slack-webhook-url', '#channel');
    }

    /**
     * Register the Horizon gate.
     *
     * This gate determines who can access Horizon in non-local environments.
     *
     * Previously the email allowlist was an empty array literal, which meant
     * the gate denied EVERYONE in staging/production — no admin could view
     * the queue dashboard when they actually needed it. Now the allowlist is
     * populated from the HORIZON_ADMIN_EMAILS env var (comma-separated). If
     * unset we fail closed (empty allowlist, no access) rather than falling
     * back to a hard-coded email, so a deploy that forgot to set the var
     * doesn't accidentally expose the dashboard to the wrong person.
     */
    protected function gate(): void
    {
        // Pre-normalised list<string> from config/services.php — already
        // lowercased, trimmed, empties dropped, reindexed.  Empty array on
        // unset HORIZON_ADMIN_EMAILS = no access (fail closed by design).
        $allowed = (array) config('services.horizon.admin_emails', []);

        Gate::define('viewHorizon', function ($user = null) use ($allowed): bool {
            $email = optional($user)->email;
            if ($email === null) {
                return false;
            }
            return in_array(strtolower($email), $allowed, true);
        });
    }
}
