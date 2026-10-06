"""Shared helpers for merging vendor-profile column mappings into the
hardcoded COLUMN_ALIASES dictionaries used by the csv_* parsers.

CC-02 Item 6 (2026-05-23): the vendor_profile + column_mapping tables
in Laravel have existed since 2026-04-18 but no parser actually
consumed them at ingest time. This module is the consumption layer.

Every csv_* parser (and the xlsx sheet router) accepts ``vendor_aliases=``;
``ingest_tabular._vendor_aliases_for`` builds the dict from the per-upload
column map. Vendor spellings are placed AHEAD of the built-in aliases.
"""
from __future__ import annotations


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

