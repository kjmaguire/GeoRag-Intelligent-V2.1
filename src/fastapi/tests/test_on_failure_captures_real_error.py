"""
2026-08-16 — regression coverage for the ingest_pdf / ingest_zip_archive
on_failure hooks capturing the REAL upstream exception.

Background
----------
Both workflows' on_failure_task hooks previously wrote a hardcoded
placeholder string ("ingest_pdf workflow failure hook fired" /
"ingest_zip_archive workflow failure hook fired") into
silver.ingest_progress.error_text / silver.archive_ingest_runs.error_text
on EVERY failure — worker crash, cancellation, or a genuine bug — making
it impossible to root-cause a recurring failure from the IngestionRuns UI
or a DB query alone. Surfaced live: 7 ingest_pdf failures over 7 days, all
recorded with this identical uninformative string.

The fix reads Hatchet's `ctx.task_run_errors` (a dict of
{task_name: error_message} populated specifically for on_failure hooks,
per hatchet_sdk.context.Context.task_run_errors) and stores the real
per-task error instead.

Style note: these are pure source-inspection tests, matching the existing
convention in test_ingest_zip_archive_observability.py — the Hatchet
Client bootstraps from a JWT that encodes server_url/grpc_broadcast_address,
which makes constructing a working dummy client (and therefore importing
the decorated Task object to invoke directly) impractical in a unit test.
"""
from __future__ import annotations

import pathlib

INGEST_PDF_PATH = (
    pathlib.Path(__file__).parents[1] / "app" / "hatchet_workflows" / "ingest_pdf.py"
)
INGEST_ZIP_ARCHIVE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "app"
    / "hatchet_workflows"
    / "ingest_zip_archive.py"
)


def _on_failure_body(source: str) -> str:
    """Extract the on_failure function body (from its def line to the next
    top-level def/EOF) so assertions don't accidentally match an unrelated
    part of the file."""
    start = source.index("async def on_failure(")
    rest = source[start:]
    end = rest.find("\n\n\n")  # blank-line-delimited section break
    return rest[: end if end != -1 else len(rest)]


def test_ingest_pdf_on_failure_reads_task_run_errors():
    body = _on_failure_body(INGEST_PDF_PATH.read_text(encoding="utf-8"))

    assert "ctx.task_run_errors" in body, (
        "ingest_pdf.on_failure must read ctx.task_run_errors to capture the "
        "real upstream exception — without it every failure (worker crash, "
        "cancellation, or a real bug) records the same uninformative "
        "placeholder string, making recurring failures un-diagnosable from "
        "the IngestionRuns UI or a DB query alone."
    )
    assert '"ingest_pdf workflow failure hook fired"' not in body, (
        "the old hardcoded placeholder error string must not be written "
        "unconditionally — it should only ever appear as part of the "
        "no-captured-error fallback message, not as the primary error value."
    )


def test_ingest_zip_archive_on_failure_reads_task_run_errors():
    body = _on_failure_body(INGEST_ZIP_ARCHIVE_PATH.read_text(encoding="utf-8"))

    assert "ctx.task_run_errors" in body, (
        "ingest_zip_archive.on_failure must read ctx.task_run_errors to "
        "capture the real upstream exception, same fix as ingest_pdf.on_failure."
    )
    assert '"ingest_zip_archive workflow failure hook fired"' not in body, (
        "the old hardcoded placeholder error string must not be written "
        "unconditionally."
    )


def test_both_hooks_have_a_fallback_when_no_errors_are_captured():
    """Worker SIGKILL / cancellation can leave task_run_errors empty (no
    Python exception was ever raised to capture) — the hook must still
    write something diagnostic, not crash or write an empty string."""
    for path in (INGEST_PDF_PATH, INGEST_ZIP_ARCHIVE_PATH):
        body = _on_failure_body(path.read_text(encoding="utf-8"))
        assert "if task_errors" in body and "else" in body, (
            f"{path.name}: on_failure must branch on whether task_run_errors "
            "came back empty and still produce a diagnostic fallback message."
        )
