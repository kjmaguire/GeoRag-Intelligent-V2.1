"""Geology file-format parsers, shared by the Hatchet ingestion workflows.

These parsers were written for the Dagster Bronze→Silver pipeline and carry
the most detailed behavioural history in the ingestion path — CSV delimiter
and decimal-comma detection, XLSX multi-sheet dispatch, spatial CRS confidence
scoring, hole-ID canonicalisation, dip-convention detection and unit-ambiguity
review flagging each came out of a specific audit.

That history used to be described here as "the most thoroughly exercised code
in the ingestion path", which was not true of THIS package. The test suites it
referred to live in ``src/dagster/tests`` (57 files), they exercise the frozen
copy rather than this one, and their CI steps were removed on 2026-08-11 — so
for ten days the claim rested on tests that did not run against code that was
not called. On 2026-08-21 eight of those files were ported into
``tests/`` here, repointed at this package: see the header on any of them.

Dagster went dormant on 2026-07-28 and its upload categories were answered
with `422 retired_pipeline` from then on, so every geology format except PDF
and TIFF stopped being ingestible. The parsers themselves never stopped being
correct — they simply had no caller. This package gives them one.

Relationship to ``src/dagster/georag_dagster/parsers``
-----------------------------------------------------
This is a CURATED COPY, not a move, and that is deliberate: the Dagster tree
is dormant by explicit decision and is not to be modified. Two modules are
left behind rather than copied:

  * ``pdf_report`` — already deleted; the PDF path lives in the FastAPI §04p
    stack now. Note that ``georag_dagster/parsers/__init__.py`` still imports
    it, so that package does not import at all today.
  * ``docx_parser`` and ``segy_parser`` — the first depends on ``pdf_report``,
    the second on ``segyio``, which is not in the FastAPI image.

The duplication is a real cost and is recorded here rather than hidden. It was
argued to be tolerable because the Dagster copy is frozen, so the two "cannot
silently diverge" — and that argument is already dead. Freezing one copy stops
IT from moving; it does nothing about this one. Measured 2026-08-21:

  * ``spatial_parser`` diverged substantively. ``FEATURE_TYPES`` and the
    ordered ``_TYPE_RULES`` table were added here on 2026-08-20 to stop the
    classifier returning values that ``chk_spatial_features_type`` rejects.
    The frozen copy still returns "alteration", "target" and "feature", and
    still has the bug.
  * ``csv_collar`` gained a log line that records the stable reason CODE
    instead of the free-text reason, which interpolates raw cell values.
  * ``_hole_id`` and ``_encoding`` are byte-identical; ``_dip_convention``
    differs only in line endings; the rest differ only in import paths.

So the two copies are one real bug fix apart, in the direction that matters:
the tests that were still running belonged to the copy that was still
broken. Collapsing them into this package (with the Dagster
tree importing from here) is the right end state and should happen the moment
anyone touches Dagster again.
"""

from georag_geoparsers.csv_collar import parse_csv_collars
from georag_geoparsers.csv_geochronology import parse_csv_geochronology
from georag_geoparsers.csv_lithology import parse_csv_lithology
from georag_geoparsers.csv_sample import parse_csv_samples
from georag_geoparsers.csv_survey import parse_csv_surveys

__all__ = [
    "parse_csv_collars",
    "parse_csv_geochronology",
    "parse_csv_lithology",
    "parse_csv_samples",
    "parse_csv_surveys",
]
