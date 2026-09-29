<?php

declare(strict_types=1);

namespace App\Policies;

use App\Models\Project;
use App\Models\User;

/**
 * Who may start which Hatchet workflow from the product (HAT-13, 2026-09-29).
 *
 * Before this, eight registered workflows could only be started from the
 * Hatchet UI. Each now has a trigger, and this class is the only place that
 * decides who may pull it. There are three tiers:
 *
 *   PROJECT_WORKFLOWS    any member of the project. These are the geologist's
 *                        own actions on their own project.
 *   WORKSPACE_WORKFLOWS  an admin who is also a member of a project in that
 *                        workspace. They are operator actions (export,
 *                        restore, lineage, support), and the workspace
 *                        membership keeps a global admin flag from reaching
 *                        into a tenant they have no part in.
 *   PLATFORM_WORKFLOWS   any admin. There is no tenant to scope to.
 *
 * FastAPI keeps its own copy of the list (TRIGGERS in
 * src/fastapi/app/routers/workflow_trigger.py), and
 * tests/test_workflow_trigger.py fails if the two ever name different
 * workflows. FastAPI also checks that every resource the input names lives
 * inside the workspace authorised here.
 *
 * Deliberately absent: field_outcome_learning (manual; nothing writes
 * targeting.target_outcomes and each run appends backtests),
 * continuous_learning_loop (a cron since 2026-09-29) and nl_summaries
 * (manual by design, see its module docstring).
 */
final class WorkflowTriggerPolicy
{
    public const PROJECT_WORKFLOWS = ['generate_report', 'score_targets'];

    public const WORKSPACE_WORKFLOWS = [
        'workspace_export',
        'restore_workspace',
        'lineage_walk',
        'support_packet_assemble',
        'support_replay',
    ];

    public const PLATFORM_WORKFLOWS = ['llm_incident_diagnosis_run'];

    public function triggerForProject(User $user, Project $project, string $workflow): bool
    {
        return in_array($workflow, self::PROJECT_WORKFLOWS, true)
            && $user->hasProjectAccess((string) $project->project_id);
    }

    public function triggerForWorkspace(User $user, string $workspaceId, string $workflow): bool
    {
        return in_array($workflow, self::WORKSPACE_WORKFLOWS, true)
            && (bool) $user->is_admin
            && $user->hasWorkspaceAccess($workspaceId);
    }

    public function triggerForPlatform(User $user, string $workflow): bool
    {
        return in_array($workflow, self::PLATFORM_WORKFLOWS, true)
            && (bool) $user->is_admin;
    }
}
