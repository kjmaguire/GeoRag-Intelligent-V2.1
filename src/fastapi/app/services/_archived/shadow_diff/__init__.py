"""Phase 1 Step 5B — shadow diff classifier package.

Implements the locked diff contract from
``docs/phase1_v149_ingest_pdf_survey.md`` §10. Pure-Python; no I/O.
"""

from app.services.shadow_diff.classifier import (
    Classification,
    DiffOutcome,
    classify_shadow_run,
)

__all__ = ["Classification", "DiffOutcome", "classify_shadow_run"]
