<?php

declare(strict_types=1);

namespace App\Support;

/**
 * The canonical join-key form of a drill-hole id.
 *
 * Mirrors, character for character, the one rule the rest of the platform
 * uses: `georag_geoparsers._hole_id.canonicalize` (Python) and
 * `silver.canonical_hole_id(text)` (SQL, which the
 * `trg_collars_hole_id_canonical` trigger applies to every collar write):
 * trim, drop the separators space / hyphen / underscore / dot / slash,
 * uppercase, and an empty result is null.
 *
 *   LEB-23-001, leb_23_001, " LEB 23/001" -> LEB23001
 *
 * `silver.collars` is unique on `(project_id, hole_id_canonical)` (§04e,
 * SME-approved 2026-09-29), so two spellings of one hole are one collar.
 */
final class HoleId
{
    public static function canonicalize(?string $holeId): ?string
    {
        if ($holeId === null) {
            return null;
        }

        $stripped = preg_replace('/[ \-_.\/]+/', '', trim($holeId)) ?? '';

        return $stripped === '' ? null : strtoupper($stripped);
    }
}
