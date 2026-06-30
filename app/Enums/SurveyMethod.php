<?php

declare(strict_types=1);

namespace App\Enums;

/**
 * Downhole survey instrument family (per §04e Downhole Survey schema).
 *
 * `Gyro`        — north-seeking gyro, immune to magnetic interference.
 *                 Standard for deep production drilling and any hole
 *                 within ~300 m of magnetic mineralisation.
 * `Magnetic`    — single- or multi-shot magnetic compass. Cheap, fast,
 *                 unreliable inside or near magnetic ore.
 * `Multishot`   — multi-station tool (gyro or magnetic) recording
 *                 azimuth+dip at fixed depth intervals during a single
 *                 trip. The recorded series is what populates this
 *                 schema's per-row entries.
 *
 * Industry-standard closed vocabulary. A new survey instrument family
 * (e.g. north-seeking solid-state gyro) would warrant a new enum value
 * + schema discussion, not a runtime SmeConfig extension.
 */
enum SurveyMethod: string
{
    case Gyro = 'Gyro';
    case Magnetic = 'Magnetic';
    case Multishot = 'Multishot';
}
