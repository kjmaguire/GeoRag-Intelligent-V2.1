# Phase 67 Handoff — Corpus labeling SME guide

**Status:** Complete. Pure docs; no code change.

## What landed

- `tests/fixtures/phase3_pdf_corpus/KYLE_LABELING_GUIDE.md` — guided
  workflow doc that complements the existing `LABELING_TRACKER.md`
  and `README.md`
- `LABELING_TRACKER.md` updated with a pointer to the new guide

## Contents of KYLE_LABELING_GUIDE.md

1. **Integration explanation**: how each PDF + label JSON gets
   consumed by the (still-to-be-written) `phase3_master_plan_acceptance.sh`
2. **Time budget**: ~10-25 min per PDF, 8-20 hours for the full 50;
   suggests starting with 25 (5 per profile) for v1 acceptance
3. **Recommended labeling order** (easiest → hardest by SME effort):
   - Native (~15 min each) — pre-vetted SEDAR+ NI 43-101 companies
   - Table-heavy (~10 min each) — Section 14 excerpts from native PDFs
   - Map-heavy (~5 min each) — GSC + SK Geological Survey maps
   - Mixed (~25 min each, hardest) — older NI 43-101s with scanned exhibits
   - Scanned (~15 min each) — SK Assessment Files pre-1990
4. **Pre-vetted source URLs** per profile (SEDAR+ company list,
   GSC publications, SK assessment file system)
5. **Label JSON quick reference** + pointer to `_label_schema.json`
6. **Workflow at 8am** — step-by-step playbook
7. **What's tested vs what Step 9 needs** — gap analysis pointing
   at the missing `phase3_master_plan_acceptance.sh` script
   (~150 lines of Python; noted as the first 8am task)
8. **Profile classifier threshold tuning** — explains the 5 constants
   in `profile.py` likely to need adjustment during labeling

## Why this matters

Step 9 is the last work-block before §3 closeout. Without the
50-PDF corpus passing, RAGFlow can't be retired (Step 10). The
guide reduces the per-PDF cognitive load + sourcing time so the
labeling can run efficiently rather than as an open-ended search.

Realistic estimate for SME work after the guide: 8-20 hours total,
spread across 2-3 sessions. With the path well-marked, no
calendar-eating "where do I start?" friction.

## Carry-overs added

- `scripts/phase3_master_plan_acceptance.sh` — the actual acceptance
  script doesn't exist yet (~150 lines of Python orchestrating
  `orchestrate()` calls + label comparisons). Suggested as the first
  8am task in the guide.

## Master-plan §3 progress unchanged. Step 9 is now better-supported
for the SME labeling pass, but still requires Kyle's time.
