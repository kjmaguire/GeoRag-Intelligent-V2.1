"""Silver → gold aggregates for the drillhole stack (2026-05-20).

Each asset reads from silver.* and UPSERTs into gold.*. All
aggregation is SQL-side — no Python row iteration — so the assets
scale linearly with corpus size and re-run cheaply.

  assay_composites.py          → gold.assay_composites
  significant_intersections.py → gold.significant_intersections
  drill_summaries.py           → gold.drill_summaries
  zone_statistics.py           → gold.zone_statistics
  qaqc_statistics.py           → gold.qaqc_statistics
  element_correlations.py      → gold.element_correlations
  campaign_summaries.py        → gold.campaign_summaries
"""
