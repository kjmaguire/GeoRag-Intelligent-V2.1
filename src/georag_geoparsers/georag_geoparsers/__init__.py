"""Geology file-format parsers, shared by the Hatchet ingestion workflows.

These parsers were written for the Dagster Bronze→Silver pipeline and are the
most thoroughly exercised code in the ingestion path — CSV delimiter and
decimal-comma detection, XLSX multi-sheet dispatch, spatial CRS confidence
scoring, hole-ID canonicalisation, dip-convention detection and unit-ambiguity
review flagging all have their own audits and test suites behind them.

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

The duplication is a real cost and is recorded here rather than hidden. It is
tolerable only because the Dagster copy is frozen: nothing runs it, so the two
cannot silently diverge. Collapsing them into this package (with the Dagster
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
