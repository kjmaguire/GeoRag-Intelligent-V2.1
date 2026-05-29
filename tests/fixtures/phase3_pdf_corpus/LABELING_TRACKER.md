# Phase 3 Corpus Labeling Tracker

> **For the SME (Kyle):** see `KYLE_LABELING_GUIDE.md` in this directory
> for a guided workflow including pre-vetted source URLs per profile,
> time estimates, and recommended labeling order. The doc was prepped
> during the overnight autonomous run (doc-phase 67) to make morning
> work efficient.

Track per-PDF labeling progress here. The acceptance test reads
`*.label.json` files, not this tracker — this is just for human
session planning.

## Status legend

- [ ] PDF not yet sourced
- [s] PDF sourced, label not written
- [x] PDF + label complete

## Native (10 / pdfminer.six + pdfplumber path)

- [ ] 1. ___
- [ ] 2. ___
- [ ] 3. ___
- [ ] 4. ___
- [ ] 5. ___
- [ ] 6. ___
- [ ] 7. ___
- [ ] 8. ___
- [ ] 9. ___
- [ ] 10. ___

**Sourcing notes:** SEDAR+ NI 43-101 reports from 2020+. Spread across
Cameco, NexGen, Denison, IsoEnergy, Fission, Skyharbour, or similar.
Aim for variety in commodity (≥6 uranium, ≥2 gold, ≥2 lithium/other).

## Scanned (10 / PaddleOCR PP-OCRv5 path)

- [ ] 1. ___
- [ ] 2. ___
- [ ] 3. ___
- [ ] 4. ___
- [ ] 5. ___
- [ ] 6. ___
- [ ] 7. ___
- [ ] 8. ___
- [ ] 9. ___
- [ ] 10. ___

**Sourcing notes:** SK assessment files (SMDI/SMAD) pre-1990.
Saskatchewan claim archives. Older + lower-quality scan + at least 2
with hand-written annotations.

## Mixed (10 / Docling layout-first path)

- [ ] 1. ___
- [ ] 2. ___
- [ ] 3. ___
- [ ] 4. ___
- [ ] 5. ___
- [ ] 6. ___
- [ ] 7. ___
- [ ] 8. ___
- [ ] 9. ___
- [ ] 10. ___

**Sourcing notes:** Older NI 43-101s (2010-2015) with scanned exhibits
embedded. Property option agreements with scanned signature pages.
The interesting case is per-page profile classification working
inside a single document.

## Table-heavy (10 / pdfplumber + Docling table focus)

- [ ] 1. ___
- [ ] 2. ___
- [ ] 3. ___
- [ ] 4. ___
- [ ] 5. ___
- [ ] 6. ___
- [ ] 7. ___
- [ ] 8. ___
- [ ] 9. ___
- [ ] 10. ___

**Sourcing notes:** Resource estimate technical reports with 20+
pages of grade-tonnage tables. NI 43-101 Section 14 excerpts. Drillhole
assay summary appendices. At least 2 should have multi-page tables
that span page boundaries (tests row-continuation detection).

## Map-heavy (10 / always route to Silver Review v1 deferral)

- [ ] 1. ___
- [ ] 2. ___
- [ ] 3. ___
- [ ] 4. ___
- [ ] 5. ___
- [ ] 6. ___
- [ ] 7. ___
- [ ] 8. ___
- [ ] 9. ___
- [ ] 10. ___

**Sourcing notes:** GSC / Saskatchewan Geological Survey published
maps. Government claim maps. NI 43-101 figure-only appendices.
Fold-out plates. Acceptance test verifies these route to review,
not that they parse correctly.

## Session log

Track labeling sessions here for time-budget tracking:

| Date | PDFs labeled | Hours | Notes |
|---|---|---|---|
|  |  |  |  |

## Reduce-scope option

If 50 PDFs proves too heavy for the labeling budget, reduce to 25
(5 per profile) for v1 acceptance:

- [ ] Confirm with implementation lead before reducing
- [ ] Document the reduction in `docs/phase3_master_plan_handoff.md`
- [ ] Note: §9.8 XGBoost classifier training corpus needs to grow
      to 1,000 reviewed pages independently of this acceptance corpus
