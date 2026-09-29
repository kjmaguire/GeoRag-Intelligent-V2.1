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
 *  - mineralization gold `mineralization` rows (mineralization_payload; §04e,
 *                   SME-approved 2026-09-29). Gold holds ONE row per interval
 *                   with every mineral of that interval in the payload; this
 *                   class flattens them back to one band per mineral, the shape
 *                   the strip log draws (a silver.mineralization row per
 *                   mineral). silver.mineralization is no longer read here -
 *                   it stays the raw record behind the collar API.
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
     * One band per mineral, flattened out of the gold `mineralization` rows.
     *
     * A gold row is one interval and carries every mineral logged over it
     * (`mineralization_payload.minerals`, in silver created_at, id order); the
     * strip log wants one band per mineral, so they are unpacked here in that
     * order, interval by interval.
     *
     * The cap is on the FLATTENED bands, not on gold rows: a single interval
     * with many minerals must not slip past it. Rows are fetched
     * MAX_INTERVALS_PER_TRACK + 1 at a time - every row yields at least one
     * band unless its payload is unreadable - and `truncated` is set when the
     * flattened list overflows the cap OR when the row limit itself was hit
     * (there may be more rows than were read).
     *
     * @return array{bands: list<array<string, mixed>>, truncated: bool}
     */
    public function mineralization(string $collarId): array
    {
        $rows = DB::table('gold.drillhole_intervals_visual')
            ->where('collar_id', $collarId)
            ->where('interval_kind', 'mineralization')
            ->orderBy('depth_from')
            ->limit(self::MAX_INTERVALS_PER_TRACK + 1)
            ->get(['depth_from', 'depth_to', 'mineralization_payload']);

        $truncated = $rows->count() > self::MAX_INTERVALS_PER_TRACK;

        $bands = [];
        foreach ($rows as $r) {
            foreach ($this->mineralList($r->mineralization_payload) as $mineral) {
                $bands[] = [
                    'from' => (float) $r->depth_from,
                    'to' => (float) $r->depth_to,
                ] + $mineral;
            }
        }

        if (count($bands) > self::MAX_INTERVALS_PER_TRACK) {
            $truncated = true;
            $bands = array_slice($bands, 0, self::MAX_INTERVALS_PER_TRACK);
        }

        return ['bands' => $bands, 'truncated' => $truncated];
    }

    /**
     * Collars (of the given set) that have anything to draw beyond curves:
     * a gold lithology, alteration or mineralization band.
     *
     * One query, not one per collar - the LOGS hole picker asks this for a
     * whole project.
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

        return DB::table('gold.drillhole_intervals_visual')
            ->whereIn('collar_id', $collarIds)
            ->whereIn('interval_kind', ['lithology', 'alteration', 'mineralization'])
            ->distinct()
            ->pluck('collar_id')
            ->map(fn ($id) => (string) $id)
            ->unique()
            ->values()
            ->all();
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
     * The minerals of one gold `mineralization` row, in the per-mineral band
     * shape the front end consumes (minus from/to, which are the row's).
     *
     * @return list<array{mineral: string, abundance_pct: ?float, form: ?string, grain_size: ?string, notes: ?string}>
     */
    private function mineralList(mixed $payload): array
    {
        $decoded = is_string($payload) ? json_decode($payload, true) : $payload;
        $items = is_array($decoded) ? ($decoded['minerals'] ?? []) : [];
        if (! is_array($items)) {
            return [];
        }

        $out = [];
        foreach ($items as $item) {
            if (! is_array($item) || ! isset($item['mineral'])) {
                continue;
            }
            $out[] = [
                'mineral' => (string) $item['mineral'],
                'abundance_pct' => isset($item['abundance_pct']) && is_numeric($item['abundance_pct'])
                    ? (float) $item['abundance_pct']
                    : null,
                'form' => isset($item['form']) ? (string) $item['form'] : null,
                'grain_size' => isset($item['grain_size']) ? (string) $item['grain_size'] : null,
                'notes' => isset($item['notes']) ? (string) $item['notes'] : null,
            ];
        }

        return $out;
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
