<?php

declare(strict_types=1);

namespace App\Services\Collars;

/**
 * Which north a drill azimuth is measured from, and the azimuth it comes to
 * relative to TRUE north — the north of the Workspace 3D frame.
 *
 * GIS audit 2026-10. promote_silver_to_gold applies a DECLARED reference
 * (a survey station's own `silver.surveys.azimuth_reference`, else the
 * project's `orientation_reference`) before it desurveys a trace
 * (FastAPI app/services/ingest/azimuth_reference.py, GIS-12). The 3D views
 * draw from the raw stations, so a hole whose azimuths are magnetic, or grid
 * north of a non-local projection, was drawn rotated against its own map
 * trace by the whole declination or convergence. WorkspaceController runs
 * every declared station through here so the 3D frame (east/north in metres
 * about the collars' centroid, north = true north) and the map agree.
 *
 * What is converted, and to what — `azimuth` here is the recorded one:
 *
 *   true      the azimuth already IS relative to true north: unchanged.
 *   magnetic  true = magnetic + declination (degrees, EAST positive, from the
 *             project). With no declination nothing can be applied: the
 *             azimuth is returned as recorded and flagged `unapplied`, never
 *             guessed.
 *   grid      true = grid - theta, where theta is the clockwise angle from
 *             grid north to true north in that grid at the collar. The grid
 *             is the project's CRS when it is a different projection from the
 *             collar's own UTM zone, else the collar's own zone — exactly the
 *             choice promote makes. The caller supplies theta.
 *   none      nothing declared (BOH / TOH are the core-orientation mark and
 *             declare nothing): the azimuth is used as recorded.
 *
 * NOT done here, deliberately, because it is Kyle's call (2026-09-29: "the
 * DEFAULT is unchanged — no convergence or declination is applied — unless
 * the data DECLARES its azimuth reference"): a hole with NO declaration is
 * drawn at its recorded azimuth, read as true north. promote reads the same
 * numbers as grid north of the collar's UTM zone, so the map trace of an
 * undeclared hole differs from its 3D drawing by the convergence (about 2.5
 * degrees at 58 N, 3 degrees from the central meridian).
 *
 * The spellings are the Python table in
 * georag_geoparsers/_azimuth_reference.py; a FastAPI test fails if the two
 * drift.
 *
 * Octane: pure and stateless.
 */
final class SurveyAzimuthReference
{
    public const TRUE = 'true';

    public const MAGNETIC = 'magnetic';

    public const GRID = 'grid';

    /**
     * Spelling -> canonical reference. Mirrors _SPELLINGS in
     * georag_geoparsers/_azimuth_reference.py.
     *
     * @var array<string, list<string>>
     */
    public const SPELLINGS = [
        self::TRUE => ['true', 'true_north', 'truenorth', 'tn', 't', 'geographic', 'geographic_north'],
        self::MAGNETIC => ['magnetic', 'magnetic_north', 'magneticnorth', 'mag', 'mag_north', 'mn', 'm'],
        self::GRID => ['grid', 'grid_north', 'gridnorth', 'gn', 'g'],
    ];

    /**
     * 'true' | 'magnetic' | 'grid' for a recognised spelling, else null.
     *
     * null, blank and the core-orientation marks (BOH / TOH) all return null:
     * none of them says which north an azimuth is measured from. An
     * unrecognised value is not read as grid either — that would make an
     * unreadable declaration indistinguishable from an absent one.
     */
    public static function canonical(?string $raw): ?string
    {
        if ($raw === null) {
            return null;
        }
        $token = str_replace('.', '', str_replace(['-', ' '], '_', strtolower(trim($raw))));
        if ($token === '') {
            return null;
        }
        foreach (self::SPELLINGS as $canonical => $spellings) {
            if (in_array($token, $spellings, true)) {
                return $canonical;
            }
        }

        return null;
    }

    /**
     * The recorded azimuth relative to true north.
     *
     * @param string|null $reference Canonical (see canonical()); null = undeclared.
     * @param float|null $declination Degrees, east positive; needed for 'magnetic'.
     * @param float|null $gridTrueBearing Clockwise degrees from grid north to true north in the
     *                                    grid the azimuth is relative to; needed for 'grid'.
     *
     * @return array{azimuth: float, unapplied: bool} `unapplied` is true when a reference was
     *                                                declared but could not be applied; the azimuth is
     *                                                then the recorded one.
     */
    public static function toTrueNorth(
        float $azimuth,
        ?string $reference,
        ?float $declination,
        ?float $gridTrueBearing,
    ): array {
        switch ($reference) {
            case self::TRUE:
                return ['azimuth' => $azimuth, 'unapplied' => false];
            case self::MAGNETIC:
                if ($declination === null) {
                    return ['azimuth' => $azimuth, 'unapplied' => true];
                }

                return ['azimuth' => self::wrap($azimuth + $declination), 'unapplied' => false];
            case self::GRID:
                if ($gridTrueBearing === null) {
                    return ['azimuth' => $azimuth, 'unapplied' => true];
                }

                return ['azimuth' => self::wrap($azimuth - $gridTrueBearing), 'unapplied' => false];
            default:
                return ['azimuth' => $azimuth, 'unapplied' => false];
        }
    }

    /** Wrap to [0, 360). */
    private static function wrap(float $degrees): float
    {
        $wrapped = fmod($degrees, 360.0);

        return $wrapped < 0.0 ? $wrapped + 360.0 : $wrapped;
    }
}
