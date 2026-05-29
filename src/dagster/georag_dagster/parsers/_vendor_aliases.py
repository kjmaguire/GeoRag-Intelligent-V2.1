"""Shared helpers for merging vendor-profile column mappings into the
hardcoded COLUMN_ALIASES dictionaries used by the csv_* parsers.

CC-02 Item 6 (2026-05-23): the vendor_profile + column_mapping tables
in Laravel have existed since 2026-04-18 but no parser actually
consumed them at ingest time. This module is the consumption layer.

Status by parser
----------------
csv_lithology   — wired (parse_csv_lithology accepts vendor_aliases= kwarg)
csv_collar      — NOT wired yet (mirror the pattern when adopting)
csv_sample      — NOT wired yet
csv_survey      — NOT wired yet
csv_geochem     — n/a (different ingest path)

A follow-up should mirror the csv_lithology change in the other three
parsers. The seeder Database\\Seeders\\VendorProfiles\\MxDepositSeeder
seeds an MX Deposit profile with placeholder column_mappings; once a
real MX Deposit export is available from Anna, the placeholders should
be replaced with actual MX columns and the other parsers should be
wired.
"""
from __future__ import annotations

from typing import Iterable


def merge_vendor_aliases(
    base_aliases: dict[str, list[str]],
    vendor_aliases: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Return a new alias dict with vendor aliases prepended per canonical key.

    Vendor aliases take precedence over hardcoded ones (they're listed
    first in the merged value, and the alias-matching loop in
    _build_column_map returns on the first match). The base dict is not
    mutated; callers can safely keep their module-level COLUMN_ALIASES
    constant immutable.

    Empty / None ``vendor_aliases`` is a no-op — returns a shallow copy
    of ``base_aliases`` so callers always get an independent dict.
    """
    if not vendor_aliases:
        return {k: list(v) for k, v in base_aliases.items()}

    merged: dict[str, list[str]] = {}
    for canonical, base_list in base_aliases.items():
        extras = vendor_aliases.get(canonical, [])
        # Deduplicate while preserving order: vendor entries first.
        seen: set[str] = set()
        combined: list[str] = []
        for alias in list(extras) + list(base_list):
            if alias not in seen:
                seen.add(alias)
                combined.append(alias)
        merged[canonical] = combined

    # Vendor-only canonicals (not in base) get passed through as-is.
    for canonical, extras in vendor_aliases.items():
        if canonical not in merged:
            merged[canonical] = list(extras)

    return merged


def vendor_aliases_from_rows(
    rows: Iterable[dict],
    *,
    parser_type: str,
) -> dict[str, list[str]]:
    """Turn the result of `SELECT canonical_field, source_column FROM
    column_mappings WHERE vendor_profile_id = ? AND parser_type = ?` into
    the {canonical: [aliases]} shape merge_vendor_aliases expects.

    Multiple aliases for the same canonical_field are accumulated in
    column-mapping insertion order. Skips rows whose parser_type doesn't
    match the caller's expected parser_type — defensive guard so a
    misconfigured profile can't silently feed csv_collar aliases into
    csv_lithology.
    """
    result: dict[str, list[str]] = {}
    for row in rows:
        if row.get("parser_type") != parser_type:
            continue
        canonical = row.get("canonical_field")
        alias = row.get("source_column")
        if not canonical or not alias:
            continue
        result.setdefault(canonical, []).append(alias)
    return result
