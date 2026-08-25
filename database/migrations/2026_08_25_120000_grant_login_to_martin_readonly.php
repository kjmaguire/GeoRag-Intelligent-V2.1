<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * `martin_readonly` must be able to open a session.
 *
 * 2026_04_22_130000_create_silver_mvt_functions.php created it as
 * `CREATE ROLE martin_readonly NOLOGIN ...`, with a comment saying a later
 * chunk would finish configuring the role. That never happened, because
 * Martin was never actually deployed — so for four months the role held
 * EXECUTE on all 18 tile functions and could not open a session.
 *
 * It surfaced the first time Martin really started, on 2026-08-25:
 *
 *     FATAL: role "martin_readonly" is not permitted to log in
 *     SQLSTATE 28000, InitializeSessionUserId
 *
 * A NOLOGIN service role is not a security posture, it is an unfinished one:
 * nothing else in the system can use that role either, so it protects nothing
 * while guaranteeing the tile server cannot start.
 *
 * NO PASSWORD IS SET HERE, deliberately. A migration is in git and runs in
 * CD; a credential must not be either. LOGIN on its own is inert on Azure
 * Postgres — password authentication is the only mechanism enabled
 * (`activeDirectoryAuth: Disabled`), so a role with LOGIN and no password
 * still cannot connect. The password comes from
 * deploy/azure/containerapps/rotate-martin-credential.sh, which generates it,
 * sets it, and hands it to the container app without it ever touching a file,
 * a command line or a shell history.
 *
 * The live server was fixed by that script on 2026-08-25; this migration is
 * so a FRESH cluster does not inherit the same four-month gap.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (! $this->postgres()) {
            return;
        }

        // Guarded on existence rather than assumed: this migration must not
        // fail a fresh cluster whose MVT migration has been squashed away,
        // and ALTER ROLE on a missing role is a hard error.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_readonly') THEN
                    ALTER ROLE martin_readonly WITH LOGIN;
                END IF;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        if (! $this->postgres()) {
            return;
        }

        // Reversible, and honestly so: this returns the role to the state the
        // April migration left it in. It WILL stop Martin serving tiles, which
        // is the point of a down migration being an accurate inverse.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_readonly') THEN
                    ALTER ROLE martin_readonly WITH NOLOGIN;
                END IF;
            END
            $$;
        SQL);
    }

    /**
     * Roles are a Postgres concept; SQLite has none.
     *
     * The driver check comes FIRST and on its own. Reaching for
     * `information_schema` or `pg_roles` to decide would already have failed
     * on SQLite — the same trap that broke 480 tests on 2026-08-25 when a
     * migration probed `information_schema.tables` before checking the driver.
     */
    private function postgres(): bool
    {
        return Schema::getConnection()->getDriverName() === 'pgsql';
    }
};
