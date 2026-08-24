<?php

declare(strict_types=1);

return [
    /*
    |--------------------------------------------------------------------------
    | Default Workspace
    |--------------------------------------------------------------------------
    |
    | The tenant that every migration, backfill and seeder writes into, and
    | the workspace an administrator gets when bootstrapping a deployment
    | that has no projects yet.
    |
    | This UUID used to be a string literal in ProjectController::store(),
    | reached as a fallback whenever the creator had no workspace — which
    | was always, because `users` has no `workspace_id` column. That made it
    | the destination for every project any account could create, including
    | one registered seconds earlier by a stranger. It is a deployment-wide
    | constant, so it belongs in config where it can be seen and overridden;
    | reaching it is now an explicit admin path, not a silent default.
    |
    */

    'default_workspace_id' => env(
        'GEORAG_DEFAULT_WORKSPACE_ID',
        'a0000000-0000-0000-0000-000000000001',
    ),

];
