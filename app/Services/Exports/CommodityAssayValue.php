<?php

declare(strict_types=1);

namespace App\Services\Exports;

/**
 * One element's grade out of a `silver.samples.commodity_assays` object, in the
 * unit an export column wants.
 *
 * WHAT THE COLUMN HOLDS
 *     The ingestion stores `{Element}_{unit}` keys in canonical case with the
 *     unit normalised to ppm, ppb or pct (`U3O8_ppm`, `Au_ppm`, `Cu_pct`;
 *     src/georag_geoparsers/georag_geoparsers/_assay_columns.py). Gold in g/t
 *     therefore arrives as `Au_ppm`, not the `au_ppb` an export column might be
 *     named after.
 *
 *     CsaBundleExporter looked up the literal lower-case keys `u3o8_ppm`,
 *     `au_ppb` and `cu_pct`, which the ingestion never writes, so all three
 *     columns of assays.csv shipped empty for every project.
 *
 * WHAT THIS DOES
 *     Matches the key by element, case-insensitively, in any of the three units
 *     and converts to the one asked for. A key already in that unit wins and its
 *     number is returned untouched. A key that is not exactly
 *     `{element}_{ppm|ppb|pct}` is not a grade and is ignored: `U3O8_pct_e` is
 *     a radiometric equivalent (eU3O8), not a chemical assay, and `n_points` /
 *     `confidence` are bookkeeping.
 */
final class CommodityAssayValue
{
    /** Power of ten of one unit, in ppm: 1 ppb = 10^-3 ppm, 1 pct = 10^4 ppm. */
    private const EXPONENT_IN_PPM = ['ppm' => 0, 'ppb' => -3, 'pct' => 4];

    /**
     * @param array<array-key, mixed> $assays decoded commodity_assays
     * @param string $element element or oxide as written in the export, any case ("Au", "U3O8")
     * @param 'ppm'|'ppb'|'pct' $unit
     *
     * @return int|float|string|null the stored number when no conversion was needed;
     *                               a plain decimal string (no exponent) when one was; null when absent or not a number
     */
    public static function in(array $assays, string $element, string $unit): int|float|string|null
    {
        $wanted = strtolower($element);
        $converted = null;

        foreach ($assays as $key => $value) {
            if (! is_string($key) || ! is_numeric($value)) {
                continue;
            }
            if (preg_match('/^(?<element>[A-Za-z][A-Za-z0-9]*)_(?<unit>ppm|ppb|pct)$/i', $key, $parts) !== 1) {
                continue;
            }
            if (strtolower($parts['element']) !== $wanted) {
                continue;
            }

            $from = strtolower($parts['unit']);
            if ($from === $unit) {
                return $value + 0;
            }

            $converted ??= self::convert((float) $value, $from, $unit);
        }

        return $converted;
    }

    /**
     * Whole powers of ten are applied by multiplying or dividing by an exactly
     * representable power, so 0.07 ppm is 70 ppb rather than 70.00000000000001.
     * Written without an exponent: a 1.0E-7 in a CSV cell is read as text by
     * some of the tools these files are made for.
     */
    private static function convert(float $value, string $from, string $to): string
    {
        $shift = self::EXPONENT_IN_PPM[$from] - self::EXPONENT_IN_PPM[$to];
        $scaled = $shift >= 0 ? $value * (10 ** $shift) : $value / (10 ** -$shift);

        return rtrim(rtrim(sprintf('%.10F', $scaled), '0'), '.') ?: '0';
    }
}
