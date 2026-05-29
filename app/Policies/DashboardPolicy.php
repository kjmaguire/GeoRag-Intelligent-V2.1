<?php

declare(strict_types=1);

namespace App\Policies;

use App\Models\Project;
use App\Models\User;

class DashboardPolicy
{
    /**
     * Can the user view the portfolio (cross-project) dashboard?
     *
     * Requires at least one accessible project via the project_user pivot.
     */
    public function viewPortfolio(User $user): bool
    {
        return $user->projects()->exists();
    }

    /**
     * Can the user view a specific project's dashboard?
     */
    public function viewProject(User $user, Project $project): bool
    {
        return $user->hasProjectAccess($project->project_id);
    }
}
