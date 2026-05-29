<?php

declare(strict_types=1);

namespace App\Enums;

/**
 * Drilling method used for a collar (per §04e Collar schema).
 *
 * Industry-standard closed vocabulary — distinct from SME-managed
 * vocabularies like commodity codes or alteration types. Each value
 * corresponds to a physical drilling technique with characteristic
 * sample size, depth limits, and recovery quality. Adding a new value
 * here is a structural change requiring schema discussion, not a runtime
 * SmeConfig update.
 *
 * Bug history (resolved 2026-05-07): the factory previously generated
 * 'Auger' but `StoreCollarRequest` rejected it. This enum is now the
 * single source of truth; the form request and factory both reference
 * it via `Rule::enum(HoleType::class)` and `HoleType::cases()`
 * respectively, so future drift is impossible without editing this file.
 */
enum HoleType: string
{
    case Diamond    = 'Diamond';     // DD/DDH — solid core
    case RC         = 'RC';          // Reverse circulation — chip samples
    case RAB        = 'RAB';         // Rotary air blast — shallow chips
    case Rotary     = 'Rotary';      // Generic rotary
    case Percussion = 'Percussion';  // Open-hole percussion
    case Auger      = 'Auger';       // Soil/saprolite shallow sampling
    case Exploration = 'exploration'; // Generic exploration drillhole (legacy / ingested)
    case Unknown    = 'unknown';     // Catch-all for upstream rows without a method
}
