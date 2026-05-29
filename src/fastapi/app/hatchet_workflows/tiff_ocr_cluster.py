"""Phase E.1 — Hatchet workflow for cluster-scoped TIFF OCR ingestion.

Wraps the reusable :func:`app.services.ingest.tiff_ocr_ingester.ocr_cluster_tiffs`
so OCR runs can be triggered on-demand against any TIFF directory without
writing a new throwaway script for each cluster.

Originally this work was performed by a hardcoded one-off script
``src/fastapi/tmp/ocr_full_cluster.py`` baked into a manually-run
``georag-phase-e-ocr`` container for the Cameco 028N079W36 cluster
(doc-phase 182). This workflow replaces that pattern with a parameterized,
auditable Hatchet workflow that any client — Laravel, FastAPI endpoint,
hatchet-admin CLI, or the Hatchet dashboard — can invoke.

Trigger: manual only. Each invocation supplies a fresh ``cluster_dir`` +
workspace/project IDs. If recurring scheduling is ever needed, add
``on_crons=[...]`` to the workflow declaration.

Mount: the worker must see the TIFF directory at ``cluster_dir``. The
``georag-phase-b-extract`` named volume is mounted at ``/extract`` in
``georag-hatchet-worker-ingestion`` (see docker-compose.yml). Typical
invocation: ``cluster_dir="/extract/028N079W36"``.
"""
from __future__ import annotations

import logging

from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.ingest.tiff_ocr_ingester import ocr_cluster_tiffs


log = logging.getLogger("georag.hatchet.tiff_ocr_cluster")


class TiffOcrClusterInput(BaseModel):
    """Parameterizes a single OCR run over a TIFF cluster directory."""

    cluster_dir: str = Field(
        ...,
        description=(
            "Container-relative path to the directory holding the cluster's "
            "TIFFs (e.g. '/extract/028N079W36'). Must be mounted into the "
            "georag-hatchet-worker-ingestion container."
        ),
    )
    workspace_id: str = Field(
        ...,
        description="UUID of the owning workspace (sets RLS GUC for the session).",
    )
    project_id: str = Field(
        ...,
        description="UUID of the owning project — every passage is tagged with this.",
    )
    max_files: int | None = Field(
        default=None,
        description="Cap on number of files processed (None = all).",
    )
    progress_every: int = Field(
        default=25,
        description="Emit a progress log line every N files (default 25).",
    )


class TiffOcrClusterOutput(BaseModel):
    tiff_count: int
    docs_created: int
    pages_ocrd: int
    passages_inserted: int
    chars_extracted: int
    skipped: int
    skip_reasons: dict


tiff_ocr_cluster = hatchet.workflow(
    name="tiff_ocr_cluster",
    input_validator=TiffOcrClusterInput,
)


@tiff_ocr_cluster.task(execution_timeout="4h", retries=0)
async def run_ocr_cluster(
    input: TiffOcrClusterInput, ctx: Context
) -> TiffOcrClusterOutput:
    """Batch-OCR every TIFF under ``cluster_dir`` for the given workspace/project."""
    log.info(
        "tiff_ocr_cluster.start cluster_dir=%s workspace=%s project=%s max_files=%s",
        input.cluster_dir,
        input.workspace_id,
        input.project_id,
        input.max_files,
    )

    summary = await ocr_cluster_tiffs(
        input.cluster_dir,
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        max_files=input.max_files,
        progress_every=input.progress_every,
    )

    log.info("tiff_ocr_cluster.complete %s", summary)

    return TiffOcrClusterOutput(
        tiff_count=int(summary.get("tiff_count", 0)),
        docs_created=int(summary.get("docs_created", 0)),
        pages_ocrd=int(summary.get("pages_ocrd", 0)),
        passages_inserted=int(summary.get("passages_inserted", 0)),
        chars_extracted=int(summary.get("chars_extracted", 0)),
        skipped=int(summary.get("skipped", 0)),
        skip_reasons=dict(summary.get("skip_reasons", {})),
    )


__all__ = ["tiff_ocr_cluster", "TiffOcrClusterInput", "TiffOcrClusterOutput"]
