<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Collar;
use App\Support\HoleId;
use Illuminate\Database\Eloquent\Builder;
use Illuminate\Support\Facades\DB;

/**
 * The collar rows the collar-based exporters read (csv_collars, csa_bundle,
 * dxf, las_bundle), with coordinates in a CRS the file can name.
 *
 * WHY COORDINATES COME FROM geom_4326
 *     silver.collars.easting / northing are the SOURCE values, untouched: UTM
 *     of whatever zone the file used, US-foot state plane, sometimes plain
 *     lon/lat degrees. Nothing records which, so an export that wrote them out
 *     handed a modelling tool numbers it could not place -- or, worse, placed
 *     wrongly if it trusted a CRS the file never declared. geom_4326 is the one
 *     geometry every writer produces in a known frame (EPSG:4326, transformed
 *     at insert straight from the source CRS), so every coordinate here is
 *     ST_Transform(geom_4326, <epsg>) and the epsg travels with it.
 *
 * WHICH CRS
 *     The same rule as FastAPI's collar export (_COLLAR_EXPORT_SQL in
 *     routers/exports.py), so a CSV and a shapefile of one project agree:
 *       1. the project's declared crs_epsg, when that is a PROJECTED system
 *          (its spatial_ref_sys WKT is a PROJCS, so its unit is real);
 *       2. otherwise the UTM zone the collar itself sits in -- 326xx north,
 *          327xx south, longitude clamped so +180 is zone 60.
 *     Never a hard-coded zone: a fixed 32613 is what once put Alaskan holes in
 *     "UTM 13N" metres.
 *
 * Each row carries `export_easting`, `export_northing` and `export_epsg` as
 * attributes. They are NULL for a collar with no geom_4326, because a position
 * with no known CRS is exactly what this class exists to stop shipping.
 *
 * hole_type and status are free text in the table (see TolerantEnum); read them
 * with getRawOriginal(), which is what an export should say anyway.
 */
final class CollarExportQuery
{
    /**
     * EPSG of the UTM zone a WGS84 point sits in, as SQL over `geom_4326`.
     * NULL for a NULL geometry. Same expression as promote_silver_to_gold's
     * _collar_local_utm and the FastAPI export.
     */
    private const UTM_EPSG_SQL = '(CASE WHEN ST_Y(geom_4326) >= 0 THEN 32600 ELSE 32700 END
        + LEAST(60, GREATEST(1, floor((ST_X(geom_4326) + 180.0) / 6.0)::int + 1)))';

    /**
     * Collars of a project that pass the export filters, with projected
     * coordinates and their EPSG. Unordered: callers order and page them.
     *
     * `$epsg` forces one CRS for every row, for a file that can hold only one
     * (a DXF drawing). Without it the project's projected CRS applies, else each
     * collar's own UTM zone.
     *
     * @param array<string, mixed> $filters
     *
     * @return Builder<Collar>
     */
    public static function forProject(string $projectId, array $filters, ?int $epsg = null): Builder
    {
        $epsg ??= self::projectedEpsg($projectId);
        $epsg = $epsg !== null ? (string) $epsg : self::UTM_EPSG_SQL;

        return self::applyFilters(
            Collar::query()
                ->select(['collar_id', 'hole_id', 'project_id', 'elevation', 'total_depth', 'hole_type', 'azimuth', 'dip', 'drill_date', 'status'])
                ->selectRaw("ST_X(ST_Transform(geom_4326, {$epsg})) AS export_easting")
                ->selectRaw("ST_Y(ST_Transform(geom_4326, {$epsg})) AS export_northing")
                ->selectRaw("CASE WHEN geom_4326 IS NULL THEN NULL ELSE {$epsg} END AS export_epsg")
                ->where('project_id', $projectId),
            $filters,
        );
    }

    /**
     * Just the ids of those collars, for `whereIn('collar_id', ...)` on a child
     * table. A subquery rather than a list of ids: a project with more than
     * ~65,000 collars would overflow Postgres' bind-parameter limit.
     *
     * @param array<string, mixed> $filters
     *
     * @return Builder<Collar>
     */
    public static function ids(string $projectId, array $filters): Builder
    {
        return self::applyFilters(
            Collar::query()->select('collar_id')->where('project_id', $projectId),
            $filters,
        );
    }

    /**
     * The row-level filters every collar exporter understands.
     *
     * hole_type and status compare case-insensitively: the request validates
     * them against the enum vocabularies ("Active"), the ingestion writes
     * whatever the file said ("active"), and a case-sensitive match returned
     * nothing for a filter that was plainly satisfied.
     *
     * @param Builder<Collar> $query
     * @param array<string, mixed> $filters
     *
     * @return Builder<Collar>
     */
    public static function applyFilters(Builder $query, array $filters): Builder
    {
        foreach (['hole_type', 'status'] as $column) {
            if (! empty($filters[$column])) {
                $query->whereRaw("LOWER({$column}) = ?", [mb_strtolower((string) $filters[$column])]);
            }
        }

        if (! empty($filters['hole_id'])) {
            self::whereHoleId($query, (string) $filters['hole_id']);
        }

        if (! empty($filters['drill_date_from'])) {
            $query->where('drill_date', '>=', $filters['drill_date_from']);
        }
        if (! empty($filters['drill_date_to'])) {
            $query->where('drill_date', '<=', $filters['drill_date_to']);
        }
        if (isset($filters['min_depth'])) {
            $query->where('total_depth', '>=', $filters['min_depth']);
        }
        if (isset($filters['max_depth'])) {
            $query->where('total_depth', '<=', $filters['max_depth']);
        }

        return $query;
    }

    /**
     * Narrow to one hole the way GET /projects/{p}/collars?hole_id= does: the
     * display id exactly, or the same canonical id (HoleId::canonicalize), so
     * "LEB-23-001" and "leb 23 001" name the same collar.
     *
     * The collar exporters accepted `filters.hole_id` and ignored it, so a
     * one-hole export shipped the whole project; the child exporters compared
     * the display id exactly and found nothing for a differently spelled id.
     *
     * @param \Illuminate\Contracts\Database\Query\Builder $query
     * @param string $table the collars table's alias in $query ('' when unaliased)
     */
    public static function whereHoleId($query, string $holeId, string $table = ''): void
    {
        $prefix = $table !== '' ? "{$table}." : '';
        $canonical = HoleId::canonicalize($holeId);

        $query->where(function ($match) use ($prefix, $holeId, $canonical): void {
            $match->where("{$prefix}hole_id", $holeId);

            if ($canonical !== null) {
                $match->orWhere("{$prefix}hole_id_canonical", $canonical);
            }
        });
    }

    /**
     * The project's declared CRS when it is projected, else null (undeclared,
     * unknown to PostGIS, or geographic -- degrees are not what a modelling
     * tool wants for easting/northing).
     */
    public static function projectedEpsg(string $projectId): ?int
    {
        $srid = DB::table('silver.projects as p')
            ->join('public.spatial_ref_sys as s', 's.srid', '=', 'p.crs_epsg')
            ->where('p.project_id', $projectId)
            ->where('s.srtext', 'like', 'PROJCS%')
            ->value('s.srid');

        return $srid === null ? null : (int) $srid;
    }

    /**
     * The UTM zone most of the filtered collars sit in, for a file that can
     * hold only one CRS (a DXF drawing) when the project declares no projected
     * one. Null when no collar has a position. Ties go to the lower EPSG.
     *
     * @param array<string, mixed> $filters
     */
    public static function modalUtmEpsg(string $projectId, array $filters): ?int
    {
        $zone = self::applyFilters(
            Collar::query()->where('project_id', $projectId)->whereNotNull('geom_4326'),
            $filters,
        )
            ->selectRaw(self::UTM_EPSG_SQL.' AS epsg, COUNT(*) AS n')
            ->groupBy('epsg')
            ->orderByDesc('n')
            ->orderBy('epsg')
            ->limit(1)
            ->first();

        return $zone === null ? null : (int) $zone->getAttribute('epsg');
    }

    /**
     * EPSG of the UTM zone containing a WGS84 point. PHP twin of UTM_EPSG_SQL,
     * for points that never reach the database as geometry.
     */
    public static function utmEpsg(float $longitude, float $latitude): int
    {
        $zone = min(60, max(1, (int) floor(($longitude + 180.0) / 6.0) + 1));

        return ($latitude >= 0 ? 32600 : 32700) + $zone;
    }

    /**
     * The linear unit of a projected CRS, from the last UNIT[] of its WKT (the
     * first belongs to the GEOGCS and is an angle). Metres when it cannot be
     * read, which is also what every UTM zone is.
     *
     * @return array{name: string, metres_per_unit: float}
     */
    public static function linearUnit(int $epsg): array
    {
        $wkt = (string) DB::table('public.spatial_ref_sys')->where('srid', $epsg)->value('srtext');

        if (preg_match_all('/UNIT\["([^"]+)",\s*([0-9.]+(?:[eE][-+]?[0-9]+)?)/', $wkt, $units, PREG_SET_ORDER) < 1) {
            return ['name' => 'metre', 'metres_per_unit' => 1.0];
        }

        $last = end($units);

        return ['name' => $last[1], 'metres_per_unit' => (float) $last[2]];
    }

    /**
     * Project WGS84 points into a CRS in one round trip.
     *
     * @param list<array{0: float, 1: float}> $lonLat
     *
     * @return list<array{0: float, 1: float}> same order, same length
     */
    public static function project(array $lonLat, int $epsg): array
    {
        if ($lonLat === []) {
            return [];
        }

        $values = implode(', ', array_fill(0, count($lonLat), '(?::int, ?::float8, ?::float8)'));
        $bindings = [];
        foreach ($lonLat as $i => [$lon, $lat]) {
            array_push($bindings, $i, $lon, $lat);
        }

        $rows = DB::select(
            "SELECT v.i,
                    ST_X(ST_Transform(ST_SetSRID(ST_MakePoint(v.lon, v.lat), 4326), {$epsg})) AS x,
                    ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint(v.lon, v.lat), 4326), {$epsg})) AS y
               FROM (VALUES {$values}) AS v(i, lon, lat)
              ORDER BY v.i",
            $bindings,
        );

        return array_map(static fn (object $r): array => [(float) $r->x, (float) $r->y], $rows);
    }

    /**
     * A coordinate as the exports write it: millimetres, or null when the
     * collar has no position. Rounded because ST_Transform returns 15-17
     * significant digits of numerical noise.
     */
    public static function coordinate(mixed $value): ?float
    {
        return $value === null ? null : round((float) $value, 3);
    }
}
