<?php

declare(strict_types=1);

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Seed realistic-shaped structures + geochemistry rows so the per-hole
 * Analysis tab (stereonet + geochem plots) has something to render on
 * the demo project.
 *
 * The real ingestion path is Dagster → PostGIS (plan §05, M2 work).
 * This seeder is explicitly marked demo-only and idempotent — running
 * it twice is a no-op rather than a double-insert.
 *
 * Run with:
 *   docker exec georag-laravel-octane php artisan db:seed --class=DemoHoleAnalysisSeeder --force
 *
 * The numbers below are plausible for the Athabasca Basin unconformity-
 * uranium context (Triple R / PLS): pelitic/graphitic basement gneiss
 * with sandstone cover, near-vertical brittle fault zones, moderate-dip
 * foliation, weak REE enrichment. The purpose is "plots look sensible
 * to a geologist on casual inspection", not "these numbers match a
 * specific NI 43-101". Do not cite this data.
 */
class DemoHoleAnalysisSeeder extends Seeder
{
    /** @var string */
    private string $projectId = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

    public function run(): void
    {
        $collars = DB::table('silver.collars')
            ->where('project_id', $this->projectId)
            ->orderBy('hole_id')
            ->get(['collar_id', 'hole_id', 'total_depth']);

        if ($collars->isEmpty()) {
            $this->command->warn("DemoHoleAnalysisSeeder: no collars found for project {$this->projectId}; nothing to seed.");
            return;
        }

        $structuresBefore = DB::table('silver.structures')->count();
        $geochemBefore    = DB::table('silver.geochemistry')->count();

        foreach ($collars as $collar) {
            $this->seedStructuresFor($collar);
            $this->seedGeochemFor($collar);
        }

        $structuresAfter = DB::table('silver.structures')->count();
        $geochemAfter    = DB::table('silver.geochemistry')->count();

        $this->command->info(sprintf(
            'DemoHoleAnalysisSeeder: +%d structures, +%d geochem rows (across %d collars)',
            $structuresAfter - $structuresBefore,
            $geochemAfter - $geochemBefore,
            $collars->count(),
        ));
    }

    /**
     * Structural measurements: per-hole mix of foliation (shallow-
     * dipping), bedding (in the sandstone cover), brittle faults
     * (near-vertical, common in the Athabasca), joints, and a
     * lineation or two. Counts vary per hole for visual variety.
     */
    private function seedStructuresFor(object $collar): void
    {
        // Skip if this collar is already seeded (idempotency).
        $existing = DB::table('silver.structures')->where('collar_id', $collar->collar_id)->count();
        if ($existing > 0) {
            return;
        }

        // Deterministic-but-varied per hole: seed RNG from the hole_id so
        // re-running always produces the same demo dataset.
        $rng = $this->rngFromHoleId($collar->hole_id);

        $plan = [
            'bedding'   => $this->rngInt($rng, 6, 12),
            'foliation' => $this->rngInt($rng, 8, 16),
            'fault'     => $this->rngInt($rng, 2, 5),
            'joint'     => $this->rngInt($rng, 6, 14),
            'fracture'  => $this->rngInt($rng, 3, 9),
            'shear'     => $this->rngInt($rng, 1, 3),
            'vein'      => $this->rngInt($rng, 2, 6),
            'lineation' => $this->rngInt($rng, 1, 4),
        ];

        $rows = [];
        foreach ($plan as $type => $count) {
            [$dipMin, $dipMax, $azSpread, $azCenter] = $this->orientationBand($type, $rng);

            for ($i = 0; $i < $count; $i++) {
                // Core-angle measurements alpha (α) and beta (β) are the
                // raw angles read off the drill core. The orientation
                // correction turns those into true_dip + dip_direction.
                $alpha    = $this->rngFloat($rng, 15, 75);
                $beta     = $this->rngFloat($rng, 0, 360);
                $trueDip  = $this->clamp($this->rngFloat($rng, $dipMin, $dipMax), 0, 90);
                $dipDir   = $this->wrap360($azCenter + $this->rngFloat($rng, -$azSpread, $azSpread));

                $depth = $this->rngFloat($rng, 10, max(10.1, (float) $collar->total_depth - 5));

                $rows[] = [
                    'structure_id'    => (string) Str::uuid(),
                    'collar_id'       => $collar->collar_id,
                    'depth'           => round($depth, 1),
                    'structure_type'  => $type,
                    'alpha_angle'     => round($alpha, 1),
                    'beta_angle'      => round($beta, 1),
                    'true_dip'        => round($trueDip, 1),
                    'dip_direction'   => round($dipDir, 1),
                    'description'     => $this->describeStructure($type, $trueDip, $dipDir),
                    'created_at'      => now(),
                    'updated_at'      => now(),
                ];
            }
        }

        if ($rows) {
            // Chunk inserts so a large hole doesn't blow the bind limit.
            foreach (array_chunk($rows, 100) as $chunk) {
                DB::table('silver.structures')->insert($chunk);
            }
        }
    }

    /**
     * Geochemistry: sample intervals every ~20m down the hole with major
     * oxides that drift with depth (simulating a mafic→felsic transition
     * approaching the unconformity), plus computed indices (Mg#, CIA,
     * Eu anomaly) and a REE blob.
     */
    private function seedGeochemFor(object $collar): void
    {
        $existing = DB::table('silver.geochemistry')->where('collar_id', $collar->collar_id)->count();
        if ($existing > 0) {
            return;
        }

        $rng = $this->rngFromHoleId('geo-' . $collar->hole_id);
        $td  = (float) $collar->total_depth;
        $step = 20.0;

        $rows = [];
        for ($from = 10.0; $from + $step <= $td; $from += $step) {
            $to = $from + $step;

            // Depth-driven trend: SiO₂ falls from ~72% (felsic sandstone
            // cover) toward ~55% (pelitic basement) as we go deeper.
            $depthFrac = $from / max(1.0, $td);
            $sio2 = $this->clamp(72.0 - 18.0 * $depthFrac + $this->rngFloat($rng, -2.5, 2.5), 40, 80);

            // Complementary trends — MgO and Fe₂O₃ rise with depth (more
            // mafic basement), Al₂O₃ peaks mid-column (clay-rich pelite).
            $mgo   = $this->clamp(1.0 + 6.0 * $depthFrac + $this->rngFloat($rng, -0.6, 0.8), 0, 10);
            $fe2o3 = $this->clamp(2.5 + 7.0 * $depthFrac + $this->rngFloat($rng, -0.8, 1.0), 0.5, 12);
            $al2o3 = $this->clamp(14.0 + 4.0 * sin(pi() * $depthFrac) + $this->rngFloat($rng, -0.5, 0.5), 8, 22);
            $cao   = $this->clamp(1.5 + 3.0 * $depthFrac + $this->rngFloat($rng, -0.4, 0.4), 0.1, 8);
            $na2o  = $this->clamp(2.5 - 1.5 * $depthFrac + $this->rngFloat($rng, -0.3, 0.3), 0.2, 5);
            $k2o   = $this->clamp(3.0 + 1.0 * sin(2 * pi() * $depthFrac) + $this->rngFloat($rng, -0.4, 0.4), 0.5, 6);

            // Mg# = molar Mg / (Mg + Fe²⁺). Quick-and-dirty approx using
            // the oxides in weight percent and standard molecular weights.
            $mgMoles = $mgo / 40.3;
            $feMoles = 0.9 * ($fe2o3 / 159.7) * 2;  // assume 90% Fe²⁺
            $mgNumber = ($mgMoles + $feMoles) > 0
                ? 100.0 * $mgMoles / ($mgMoles + $feMoles)
                : null;

            // CIA (Chemical Index of Alteration) = Al₂O₃ / (Al₂O₃ + CaO +
            // Na₂O + K₂O) × 100. Shifts toward 70-80 in weathered pelites.
            $cia = ($al2o3 + $cao + $na2o + $k2o) > 0
                ? 100.0 * $al2o3 / ($al2o3 + $cao + $na2o + $k2o)
                : null;

            // Eu anomaly: generate REE values with a small negative Eu
            // (typical for basement gneiss with plagioclase removal).
            // Chondrite-normalised values.
            $laN  = $this->rngFloat($rng, 80, 180);
            $ceN  = $laN * $this->rngFloat($rng, 0.75, 0.95);
            $ndN  = $laN * $this->rngFloat($rng, 0.55, 0.75);
            $smN  = $laN * $this->rngFloat($rng, 0.30, 0.45);
            $euN  = $this->rngFloat($rng, 6, 18);  // suppressed ← the anomaly
            $gdN  = $laN * $this->rngFloat($rng, 0.22, 0.32);
            $dyN  = $laN * $this->rngFloat($rng, 0.14, 0.20);
            $erN  = $laN * $this->rngFloat($rng, 0.09, 0.14);
            $ybN  = $laN * $this->rngFloat($rng, 0.06, 0.10);
            $luN  = $ybN * $this->rngFloat($rng, 0.15, 0.22);

            $euStar = sqrt($smN * $gdN);
            $euAnom = $euStar > 0 ? $euN / $euStar : null;

            $rows[] = [
                'geochem_id'   => (string) Str::uuid(),
                'collar_id'    => $collar->collar_id,
                'from_depth'   => round($from, 1),
                'to_depth'     => round($to, 1),
                'sio2_wt_pct'  => round($sio2, 2),
                'al2o3_wt_pct' => round($al2o3, 2),
                'fe2o3_wt_pct' => round($fe2o3, 2),
                'mgo_wt_pct'   => round($mgo, 2),
                'cao_wt_pct'   => round($cao, 2),
                'na2o_wt_pct'  => round($na2o, 2),
                'k2o_wt_pct'   => round($k2o, 2),
                'mg_number'    => $mgNumber !== null ? round($mgNumber, 2) : null,
                'cia'          => $cia !== null ? round($cia, 2) : null,
                'eu_anomaly'   => $euAnom !== null ? round($euAnom, 3) : null,
                'ree_json'     => json_encode([
                    'La_N' => round($laN, 2),
                    'Ce_N' => round($ceN, 2),
                    'Nd_N' => round($ndN, 2),
                    'Sm_N' => round($smN, 2),
                    'Eu_N' => round($euN, 2),
                    'Gd_N' => round($gdN, 2),
                    'Dy_N' => round($dyN, 2),
                    'Er_N' => round($erN, 2),
                    'Yb_N' => round($ybN, 2),
                    'Lu_N' => round($luN, 2),
                ]),
                'created_at'   => now(),
                'updated_at'   => now(),
            ];
        }

        if ($rows) {
            DB::table('silver.geochemistry')->insert($rows);
        }
    }

    /**
     * Orientation band for each structure type: [dip_min, dip_max,
     * azimuth_spread_halfwidth, azimuth_center]. Numbers are chosen so
     * the stereonet tells a coherent structural story — bedding and
     * foliation cluster, faults are near-vertical, joints scatter.
     */
    private function orientationBand(string $type, array &$rng): array
    {
        return match ($type) {
            // Bedding in the Athabasca sandstone cover — shallow, bedding
            // dips vary a little around horizontal.
            'bedding'   => [0, 12, 45, $this->rngFloat($rng, 0, 360)],
            // Foliation in the basement — moderate dip, NE-trend clustering.
            'foliation' => [35, 55, 20, 45.0],
            // Brittle faults — near-vertical, orthogonal trend family.
            'fault'     => [75, 89, 15, $this->rngBool($rng, 0.5) ? 135.0 : 315.0],
            // Shear zones — steep but slightly less so than faults.
            'shear'     => [60, 80, 15, 135.0],
            // Joints — high dip, scattered azimuth.
            'joint'     => [70, 89, 90, $this->rngFloat($rng, 0, 360)],
            // Fractures — similar to joints but with a bit more dip range.
            'fracture'  => [45, 85, 60, $this->rngFloat($rng, 0, 360)],
            // Veins — moderate-to-steep, two orthogonal sets.
            'vein'      => [55, 80, 10, $this->rngBool($rng, 0.5) ? 60.0 : 150.0],
            // Lineations are plotted as points so dip here is the plunge.
            'lineation' => [30, 50, 20, 45.0],
            default     => [30, 70, 90, 0.0],
        };
    }

    private function describeStructure(string $type, float $dip, float $dipDir): string
    {
        $quad = $this->quadrant($dipDir);
        return sprintf('%s, %d°/%s (dip/dip-direction)', ucfirst($type), (int) round($dip), $quad);
    }

    private function quadrant(float $az): string
    {
        if ($az < 22.5 || $az >= 337.5) return 'N';
        if ($az < 67.5)   return 'NE';
        if ($az < 112.5)  return 'E';
        if ($az < 157.5)  return 'SE';
        if ($az < 202.5)  return 'S';
        if ($az < 247.5)  return 'SW';
        if ($az < 292.5)  return 'W';
        return 'NW';
    }

    // ── Deterministic PRNG helpers ────────────────────────────────────

    /** Build a tiny xorshift RNG state seeded from the hole_id string. */
    private function rngFromHoleId(string $holeId): array
    {
        $h = 0;
        foreach (str_split($holeId) as $ch) {
            $h = (($h * 31) + ord($ch)) & 0x7fffffff;
        }
        return ['state' => max(1, $h)];
    }

    private function rngFloat(array &$rng, float $min, float $max): float
    {
        // xorshift32
        $s = $rng['state'];
        $s ^= ($s << 13) & 0xffffffff;
        $s ^= ($s >> 17);
        $s ^= ($s << 5) & 0xffffffff;
        $rng['state'] = $s & 0x7fffffff;
        return $min + ($rng['state'] / 0x7fffffff) * ($max - $min);
    }

    private function rngInt(array &$rng, int $min, int $max): int
    {
        return (int) floor($this->rngFloat($rng, $min, $max + 1));
    }

    private function rngBool(array &$rng, float $pTrue = 0.5): bool
    {
        return $this->rngFloat($rng, 0, 1) < $pTrue;
    }

    private function clamp(float $v, float $lo, float $hi): float
    {
        return max($lo, min($hi, $v));
    }

    private function wrap360(float $v): float
    {
        $w = fmod($v, 360.0);
        return $w < 0 ? $w + 360.0 : $w;
    }
}
