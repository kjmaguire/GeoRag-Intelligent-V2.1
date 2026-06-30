<?php

declare(strict_types=1);

namespace App\Enums;

/**
 * Lifecycle state of an individual drill collar (per §04e Collar schema).
 *
 * Distinct from `ProjectStatus`, which tracks the overall project state
 * (active / indexing / degraded / archived). A single project can hold
 * collars in any of these three states simultaneously.
 *
 * `Active`     — drilling in progress or planned.
 * `Completed`  — drilling finished, total_depth + assays final.
 * `Abandoned`  — drilling stopped before reaching planned depth (rod
 *                lost, equipment failure, geotechnical issue). Total
 *                depth reflects what was actually drilled.
 */
enum CollarStatus: string
{
    case Active = 'Active';
    case Completed = 'Completed';
    case Abandoned = 'Abandoned';
    // Legacy / case-variant values seen in ingested data:
    case ActiveLc = 'active';
    case InProgress = 'In Progress';
    case Planned = 'Planned';
    case Unknown = 'unknown';
}
