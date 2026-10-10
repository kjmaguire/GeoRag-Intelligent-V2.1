"""The ingest tasks that call a remote service retry with a backoff.

Hatchet retries IMMEDIATELY unless a task sets ``backoff_factor``. preflight
(S3), parse (Cohere Parse), persist (Postgres and S3) and embed_verify (the embed
dispatch) all declared retries and none declared a spacing, so a transient outage
spent the whole retry budget in a few milliseconds and the run failed for a cause
that cleared seconds later (Hatchet audit 2026-10, finding 12). The parse task's
own comment said "Hatchet's retries=1 backoff kicks in -- by then memory pressure
may have eased"; there was no backoff.

This reads the declarations off the registered task objects, so it checks what
the worker registers rather than the text of the decorator.
"""
from __future__ import annotations

import pytest

from app.hatchet_workflows import ingest_pdf as pdf_mod
from app.hatchet_workflows import tiff_normalize as tiff_mod

REMOTE_CALLING_TASKS = [
    pytest.param(pdf_mod.preflight, id="ingest_pdf.preflight"),
    pytest.param(pdf_mod.parse, id="ingest_pdf.parse"),
    pytest.param(pdf_mod.persist, id="ingest_pdf.persist"),
    pytest.param(pdf_mod.embed_verify, id="ingest_pdf.embed_verify"),
    pytest.param(tiff_mod.normalize, id="tiff_normalize.normalize"),
]


@pytest.mark.parametrize("task", REMOTE_CALLING_TASKS)
def test_a_retried_remote_call_waits_before_retrying(task) -> None:
    assert task.retries >= 1
    assert task.backoff_factor is not None and task.backoff_factor > 1.0
    assert task.backoff_max_seconds is not None and task.backoff_max_seconds >= 30
