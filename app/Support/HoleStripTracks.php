<?php

declare(strict_types=1);

namespace App\Support;

use Illuminate\Support\Facades\DB;

/**
 * The tracks of one hole's strip log: lithology, alteration, mineralization.
 *
 * WHY THIS EXISTS
 *
 * Three surfaces draw a hole's geology (the Workspace LOGS panel, the compare
 * payload and the hole page), and each read `gold.drillhole_intervals_visual`
 * for `lithology` bands only. Alteration and mineralization - the minerals and
 * intensity a geologist logs - were ingested nowhere and drawn nowhere. This
 * class is the one place that turns a collar's rows into the payload the strip
 * log renders, so the surfaces cannot disagree about what a band carries.
 *
 * WHERE EACH TRACK COMES FROM
 *
 *  - lithology      gold `lithology` rows (code, label, display colour) plus the
 *                   attributes gold has no column for - grain size, hardness,
 *                   weathering, RQD, recovery, and the colour AS DESCRIBED -
 *                   read from silver.lithology_logs and matched on the
 *                   interval. Gold's own colour is a hex display colour or
 *                   nothing; the front end assigns a legend colour per code.
 *  - alteration     gold `alteration` rows (alteration_payload).
 *  - mineralization silver.mineralization directly: the gold table has no
 *                   mineralization kind (see promote_silver_to_gold), so there
 *                   is no gold row to read.
 *
 * Callers pin the workspace RLS GUC first (`withWorkspaceRls`); nothing here
 * widens or narrows that scope. Stateless on purpose - Octane keeps one
 * instance for many requests.
 */
final class HoleStripTracks
{
    /** Bands per track per hole. A hole with more than this is cut, and says so. */
    public const MAX_INTERVALS_PER_TRACK = 1500;

    private const HEX_COLOUR = '/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/';

    /**
     * @return array{
     *     lithology: list<array<string, mixed>>,
     *     alteration: list<array<string, mixed>>,
     *     mineralization: list<array<string, mixed>>,
     *     truncated: array{lithology: bool, alteration: bool, mineralization: bool}
     * }
     */
    public function forCollar(string $collarId): array
    {
        $lithology = $this->lithology($collarId);
        $alteration = $this->alteration($collarId);
        $mineralization = $this->mineralization($collarId);

        return [
            'lithology' => $lithology['bands'],
            'alteration' => $alteration['bands'],
            'mineralization' => $mineralization['bands'],
            'truncated' => [
                'lithology' => $lithology['truncated'],
                'alteration' => $alteration['truncated'],
                'mineralization' => $mineralization['truncated'],
            ],
        ];
    }

    /**
     * @return array{bands: list<array<string, mixed>>, truncated: bool}
     */
    public function lithology(string $collarId): array
    {
        $rows = DB::table('gold.drillhole_intervals_visual')
            ->where('collar_id', $collarId)
            ->where('interval_kind', 'lithology')
            ->orderBy('depth_from')
            ->limit(self::MAX_INTERVALS_PER_TRACK + 1)
            ->get(['depth_from', 'depth_to', 'lithology_code', 'lithology_label', 'color_hint']);

        $truncated = $rows->count() > self::MAX_INTERVALS_PER_TRACK;
        $details = $this->lithologyDetails($collarId);

        $bands = $rows->take(self::MAX_INTERVALS_PER_TRACK)->map(function ($r) use ($details) {
            $from = (float) $r->depth_from;
            $to = (float) $r->depth_to;
            $band = [
                'from' => $from,
                'to' => $to,
                'code' => (string) $r->lithology_code,
                'label' => (string) $r->lithology_label,
                // A display colour or ''. Never text, never a code: the front
                // end falls back to a stable legend colour per code.
                'color' => $this->displayColour($r->color_hint),
            ];
            $detail = $details[$this->intervalKey($from, $to)] ?? null;
            if ($detail !== null) {
                $band['detail'] = $detail;
            }

            return $band;
        })->values()->all();

        return ['bands' => $bands, 'truncated' => $truncated];
    }

    /**
     * @return array{bands: list<array<string, mixed>>, truncated: bool}
     */
    public function alteration(string $collarId): array
    {
        $rows = DB::table('gold.drillhole_intervals_visual')
            ->where('collar_id', $collarId)
            ->where('interval_kind', 'alteration')
            ->orderBy('depth_from')
            ->limit(self::MAX_INTERVALS_PER_TRACK + 1)
            ->get(['depth_from', 'depth_to', 'lithology_label', 'alteration_payload']);

        $truncated = $rows->count() > self::MAX_INTERVALS_PER_TRACK;

        $bands = $rows->take(self::MAX_INTERVALS_PER_TRACK)->map(function ($r) {
            return [
                'from' => (float) $r->depth_from,
                'to' => (float) $r->depth_to,
                'label' => (string) $r->lithology_label,
                'alterations' => $this->alterationList($r->alteration_payload),
            ];
        })->values()->all();

        return ['bands' => $bands, 'truncated' => $truncated];
    }

    /**
     * @return array{bands: list<array<string, mixed>>, truncated: bool}
     */
    public function mineralization(string $collarId): array
    {
        $rows = DB::table('silver.mineralization')
            ->where('collar_id', $collarId)
            ->orderBy('from_depth')
            ->orderBy('mineral')
            ->limit(self::MAX_INTERVALS_PER_TRACK + 1)
            ->get(['from_depth', 'to_depth', 'mineral', 'abundance_pct', 'form', 'grain_size', 'notes']);

        $truncated = $rows->count() > self::MAX_INTERVALS_PER_TRACK;

        $bands = $rows->take(self::MAX_INTERVALS_PER_TRACK)->map(fn ($r) => [
            'from' => (float) $r->from_depth,
            'to' => (float) $r->to_depth,
            'mineral' => (string) $r->mineral,
            'abundance_pct' => $r->abundance_pct !== null ? (float) $r->abundance_pct : null,
            'form' => $r->form !== null ? (string) $r->form : null,
            'grain_size' => $r->grain_size !== null ? (string) $r->grain_size : null,
            'notes' => $r->notes !== null ? (string) $r->notes : null,
        ])->values()->all();

        return ['bands' => $bands, 'truncated' => $truncated];
    }

    /**
     * Collars (of the given set) that have anything to draw beyond curves:
     * a lithology or alteration band in gold, or a mineralization row.
     *
     * One query per source, not one per collar - the LOGS hole picker asks
     * this for a whole project.
     *
     * @param list<string> $collarIds
     *
     * @return list<string> collar ids
     */
    public function collarsWithIntervals(array $collarIds): array
    {
        if ($collarIds === []) {
            return [];
        }

        $gold = DB::table('gold.drillhole_intervals_visual')
            ->whereIn('collar_id', $collarIds)
            ->whereIn('interval_kind', ['lithology', 'alteration'])
            ->distinct()
            ->pluck('collar_id');

        $silver = DB::table('silver.mineralization')
            ->whereIn('collar_id', $collarIds)
            ->distinct()
            ->pluck('collar_id');

        return $gold->merge($silver)->map(fn ($id) => (string) $id)->unique()->values()->all();
    }

    /**
     * The attributes gold has no column for, keyed by interval.
     *
     * @return array<string, array<string, mixed>>
     */
    private function lithologyDetails(string $collarId): array
    {
        $rows = DB::table('silver.lithology_logs')
            ->where('collar_id', $collarId)
            ->orderBy('from_depth')
            ->limit(self::MAX_INTERVALS_PER_TRACK)
            ->get([
                'from_depth', 'to_depth', 'lithology_description', 'color',
                'grain_size', 'hardness', 'weathering', 'rqd', 'recovery',
            ]);

        $out = [];
        foreach ($rows as $r) {
            $key = $this->intervalKey((float) $r->from_depth, (float) $r->to_depth);
            if (isset($out[$key])) {
                continue; // gold folds duplicate intervals into one band; so do we
            }
            $out[$key] = [
                'description' => $r->lithology_description !== null ? (string) $r->lithology_description : null,
                'colour' => $r->color !== null ? (string) $r->color : null,
                'grain_size' => $r->grain_size !== null ? (string) $r->grain_size : null,
                'hardness' => $r->hardness !== null ? (string) $r->hardness : null,
                'weathering' => $r->weathering !== null ? (string) $r->weathering : null,
                'rqd' => $r->rqd !== null ? (float) $r->rqd : null,
                'recovery' => $r->recovery !== null ? (float) $r->recovery : null,
            ];
        }

        return $out;
    }

    /** Gold stores depths at NUMERIC(10,3); match on the same scale. */
    private function intervalKey(float $from, float $to): string
    {
        return sprintf('%.3f|%.3f', $from, $to);
    }

    private function displayColour(mixed $hint): string
    {
        $value = is_string($hint) ? trim($hint) : '';

        return preg_match(self::HEX_COLOUR, $value) === 1 ? strtolower($value) : '';
    }

    /**
     * @return list<array{type: string, intensity: ?string, minerals: list<string>, notes: ?string}>
     */
    private function alterationList(mixed $payload): array
    {
        $decoded = is_string($payload) ? json_decode($payload, true) : $payload;
        $items = is_array($decoded) ? ($decoded['alterations'] ?? []) : [];
        if (! is_array($items)) {
            return [];
        }

        $out = [];
        foreach ($items as $item) {
            if (! is_array($item) || ! isset($item['type'])) {
                continue;
            }
            $minerals = is_array($item['minerals'] ?? null) ? $item['minerals'] : [];
            $out[] = [
                'type' => (string) $item['type'],
                'intensity' => isset($item['intensity']) ? (string) $item['intensity'] : null,
                'minerals' => array_values(array_map('strval', $minerals)),
                'notes' => isset($item['notes']) ? (string) $item['notes'] : null,
            ];
        }

        return $out;
    }
}
