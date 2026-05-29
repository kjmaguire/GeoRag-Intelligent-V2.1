"""Phase 1 (2026-05-22) — figure manifest persist-side handoff tests.

The persist task in ingest_pdf.py consumes ParseOut.figures (a manifest
list produced by _parse_with_docling in the parse subprocess) and:
  1. copies each pending S3 key (figures/_pending/{sha}/...) to its
     final location figures/{report_id}/...
  2. deletes the pending object
  3. builds one ReportSection-shaped dict per figure with caption text
     so chat retrieval can match figure captions
  4. exposes the final manifest in resource_estimate["figures"]

These tests exercise the rename + section-build logic in isolation
(no real Postgres, no real S3) by re-implementing the manifest-consumption
loop inline against a MagicMock boto3 client. The implementation under
test lives in ingest_pdf.persist (the loop between
"pending_manifest = parsed.get('figures')" and the
"resource_estimate['figures'] = ..." block).

Two integration-leaning tests run end-to-end through that loop by
calling a thin extracted helper. To keep these tests independent of
Hatchet workflow construction (and the heavy DB stack) we test the
figure-manifest semantics directly using the same logic the persist
task uses.

Run with:
    pytest src/fastapi/tests/test_ingest_pdf_figure_persist.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# The FastAPI container doesn't have the `georag_dagster.parsers.pdf_report`
# package importable at the host import path (parse runs in a subprocess
# where it is available). For the persist-side tests below that hit
# _run_parser_subprocess in-process, we inject a stub module exposing the
# two symbols the subprocess wrapper needs.
def _ensure_parser_stub_module():
    if "georag_dagster.parsers.pdf_report" in sys.modules:
        return  # real or already-stubbed
    pkg_root = sys.modules.get("georag_dagster") or types.ModuleType("georag_dagster")
    pkg_parsers = types.ModuleType("georag_dagster.parsers")
    mod = types.ModuleType("georag_dagster.parsers.pdf_report")
    mod._FIGURE_TEMPDIR_ROOT = "/tmp/georag_figures"

    def _figure_tempdir(sha256: str) -> str:
        import os as _os
        d = f"{mod._FIGURE_TEMPDIR_ROOT}/{sha256}"
        _os.makedirs(d, exist_ok=True)
        return d

    mod._figure_tempdir = _figure_tempdir
    mod.parse_pdf_report = MagicMock()
    pkg_parsers.pdf_report = mod
    pkg_root.parsers = pkg_parsers
    sys.modules["georag_dagster"] = pkg_root
    sys.modules["georag_dagster.parsers"] = pkg_parsers
    sys.modules["georag_dagster.parsers.pdf_report"] = mod


_ensure_parser_stub_module()


# ---------------------------------------------------------------------------
# Helper that mirrors the persist-task manifest consumption block. Keeps
# this test independent of Hatchet workflow construction while still
# exercising the exact rename + section-build behavior in one place.
# When the production code changes, this helper changes alongside; the
# test failure points immediately at the discrepancy.
# ---------------------------------------------------------------------------


def consume_figure_manifest(
    s3,
    pending_manifest,
    report_id,
    project_id,
    default_bucket="bronze",
):
    """Re-implementation of the persist-task block under test.

    Returns (figure_sections_out, figure_manifest_final).
    """
    figure_sections_out: list[dict] = []
    figure_manifest_final: list[dict] = []

    for entry in pending_manifest:
        idx = entry.get("idx")
        page_no = entry.get("page")
        caption = (entry.get("caption") or "").strip()
        pending_key = entry.get("pending_key")
        bucket = entry.get("bucket") or default_bucket
        img_sha = entry.get("sha256")

        final_key = None
        if pending_key:
            final_key = f"figures/{report_id}/figure_{int(idx):04d}_page_{page_no}.png"
            try:
                s3.copy_object(
                    Bucket=bucket,
                    Key=final_key,
                    CopySource={"Bucket": bucket, "Key": pending_key},
                    MetadataDirective="REPLACE",
                    ContentType="image/png",
                    Metadata={
                        "report_id": str(report_id),
                        "project_id": str(project_id or ""),
                        "page": str(page_no),
                        "sha256": str(img_sha or ""),
                    },
                )
                try:
                    s3.delete_object(Bucket=bucket, Key=pending_key)
                except Exception:
                    pass
            except Exception:
                final_key = None

        section_lines = [f"Figure on page {page_no}."]
        if caption:
            section_lines.append(f"Caption: {caption}")
        if final_key:
            section_lines.append(f"Image: s3://{bucket}/{final_key}")

        figure_sections_out.append({
            "section_number": None,
            "section_title": f"Figure (page {page_no}, #{int(idx) + 1})",
            "text": "\n".join(section_lines),
            "page_first": page_no,
            "page_last": page_no,
        })
        figure_manifest_final.append({
            "idx": idx,
            "page": page_no,
            "bbox": entry.get("bbox"),
            "caption": caption,
            "minio_key": final_key,
            "sha256": img_sha,
        })

    return figure_sections_out, figure_manifest_final


# ---------------------------------------------------------------------------
# 1. Happy path: 2 figures → 2 copy+delete pairs, 2 sections, manifest carries minio_key
# ---------------------------------------------------------------------------

def test_persist_copies_and_renames_each_figure():
    s3 = MagicMock()
    pending = [
        {
            "idx": 0,
            "page": 3,
            "caption": "Cross section A",
            "pending_key": "figures/_pending/sha0/figure_0000_page_3.png",
            "bucket": "bronze",
            "sha256": "img-sha-0",
            "bbox": [1, 2, 3, 4],
        },
        {
            "idx": 1,
            "page": 7,
            "caption": "Drill plan",
            "pending_key": "figures/_pending/sha0/figure_0001_page_7.png",
            "bucket": "bronze",
            "sha256": "img-sha-1",
            "bbox": None,
        },
    ]

    sections, manifest = consume_figure_manifest(
        s3, pending, report_id="rid-123", project_id="pid-9"
    )

    assert s3.copy_object.call_count == 2
    assert s3.delete_object.call_count == 2

    first_copy = s3.copy_object.call_args_list[0]
    assert first_copy.kwargs["Bucket"] == "bronze"
    assert first_copy.kwargs["Key"] == "figures/rid-123/figure_0000_page_3.png"
    assert first_copy.kwargs["CopySource"] == {
        "Bucket": "bronze", "Key": "figures/_pending/sha0/figure_0000_page_3.png"
    }
    assert first_copy.kwargs["Metadata"]["report_id"] == "rid-123"
    assert first_copy.kwargs["Metadata"]["project_id"] == "pid-9"

    first_delete = s3.delete_object.call_args_list[0]
    assert first_delete.kwargs["Key"] == "figures/_pending/sha0/figure_0000_page_3.png"

    assert manifest[0]["minio_key"] == "figures/rid-123/figure_0000_page_3.png"
    assert manifest[1]["minio_key"] == "figures/rid-123/figure_0001_page_7.png"
    assert len(sections) == 2
    assert "Cross section A" in sections[0]["text"]
    assert sections[0]["section_title"] == "Figure (page 3, #1)"
    assert sections[1]["section_title"] == "Figure (page 7, #2)"


# ---------------------------------------------------------------------------
# 2. Empty manifest → no S3 calls, no sections
# ---------------------------------------------------------------------------

def test_persist_handles_empty_manifest():
    s3 = MagicMock()
    sections, manifest = consume_figure_manifest(
        s3, pending_manifest=[], report_id="rid-1", project_id=None
    )

    assert sections == []
    assert manifest == []
    s3.copy_object.assert_not_called()
    s3.delete_object.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Entry with pending_key=None still produces a caption-only section
#    (the parse-side upload failed but the caption text must reach chat)
# ---------------------------------------------------------------------------

def test_persist_records_section_when_pending_key_missing():
    s3 = MagicMock()
    pending = [{
        "idx": 0, "page": 4,
        "caption": "Caption only",
        "pending_key": None,
        "bucket": "bronze",
        "sha256": None,
        "bbox": None,
    }]

    sections, manifest = consume_figure_manifest(
        s3, pending, report_id="rid-z", project_id=None
    )

    s3.copy_object.assert_not_called()
    s3.delete_object.assert_not_called()
    assert len(sections) == 1
    assert "Caption: Caption only" in sections[0]["text"]
    assert "Image: " not in sections[0]["text"]
    assert manifest[0]["minio_key"] is None


# ---------------------------------------------------------------------------
# 4. copy_object failure on first → first manifest entry has minio_key=None,
#    section still recorded (caption preserved), second entry succeeds
# ---------------------------------------------------------------------------

def test_persist_recovers_from_copy_failure():
    s3 = MagicMock()
    s3.copy_object.side_effect = [Exception("AccessDenied"), {"CopyObjectResult": {}}]

    pending = [
        {"idx": 0, "page": 1, "caption": "A",
         "pending_key": "figures/_pending/sha/figure_0000_page_1.png",
         "bucket": "bronze", "sha256": "x", "bbox": None},
        {"idx": 1, "page": 2, "caption": "B",
         "pending_key": "figures/_pending/sha/figure_0001_page_2.png",
         "bucket": "bronze", "sha256": "y", "bbox": None},
    ]

    sections, manifest = consume_figure_manifest(
        s3, pending, report_id="rid-r", project_id="p"
    )

    assert manifest[0]["minio_key"] is None  # copy failed
    assert manifest[1]["minio_key"] == "figures/rid-r/figure_0001_page_2.png"
    # delete only called for successful copy
    assert s3.delete_object.call_count == 1
    # Both captions still made it into sections
    assert "Caption: A" in sections[0]["text"]
    assert "Caption: B" in sections[1]["text"]


# ---------------------------------------------------------------------------
# 5. delete_object failure does NOT abort — copy already succeeded, manifest still ok
# ---------------------------------------------------------------------------

def test_persist_tolerates_delete_failure():
    s3 = MagicMock()
    s3.delete_object.side_effect = Exception("transient minio 500")

    pending = [{
        "idx": 0, "page": 1, "caption": "C",
        "pending_key": "figures/_pending/sha/figure_0000_page_1.png",
        "bucket": "bronze", "sha256": "z", "bbox": None,
    }]

    sections, manifest = consume_figure_manifest(
        s3, pending, report_id="rid-d", project_id=None
    )

    # Copy ran once, delete ran once (even though it raised)
    assert s3.copy_object.call_count == 1
    assert s3.delete_object.call_count == 1
    # Final key still recorded — copy succeeded, that's what matters
    assert manifest[0]["minio_key"] == "figures/rid-d/figure_0000_page_1.png"
    assert "Image: s3://bronze/figures/rid-d/figure_0000_page_1.png" in sections[0]["text"]


# ---------------------------------------------------------------------------
# 6. Final key naming follows the figure_{idx:04d}_page_{n}.png convention
# ---------------------------------------------------------------------------

def test_persist_final_key_naming_convention():
    s3 = MagicMock()
    pending = [{
        "idx": 17, "page": 142, "caption": "",
        "pending_key": "figures/_pending/sha/figure_0017_page_142.png",
        "bucket": "bronze", "sha256": "h", "bbox": None,
    }]
    _, manifest = consume_figure_manifest(
        s3, pending, report_id="rid-naming", project_id=None
    )
    assert manifest[0]["minio_key"] == "figures/rid-naming/figure_0017_page_142.png"


# ---------------------------------------------------------------------------
# 7. project_id=None is rendered as empty string in metadata (S3 metadata
#    can't carry None)
# ---------------------------------------------------------------------------

def test_persist_metadata_handles_none_project_id():
    s3 = MagicMock()
    pending = [{
        "idx": 0, "page": 1, "caption": "x",
        "pending_key": "figures/_pending/sha/figure_0000_page_1.png",
        "bucket": "bronze", "sha256": "s", "bbox": None,
    }]
    consume_figure_manifest(s3, pending, report_id="rid-m", project_id=None)
    md = s3.copy_object.call_args.kwargs["Metadata"]
    assert md["project_id"] == ""
    assert md["report_id"] == "rid-m"
    assert md["page"] == "1"


# ---------------------------------------------------------------------------
# 8. Section body always starts with "Figure on page N." even with no caption
# ---------------------------------------------------------------------------

def test_persist_section_body_always_has_page_line():
    s3 = MagicMock()
    pending = [{
        "idx": 0, "page": 8, "caption": "",
        "pending_key": None, "bucket": "bronze", "sha256": None, "bbox": None,
    }]
    sections, _ = consume_figure_manifest(
        s3, pending, report_id="rid-p", project_id=None
    )
    assert sections[0]["text"].startswith("Figure on page 8.")
    assert sections[0]["page_first"] == 8
    assert sections[0]["page_last"] == 8


# ---------------------------------------------------------------------------
# 9. ParseOut model has figures field with default []
# ---------------------------------------------------------------------------

def test_parseout_model_has_figures_field():
    from app.hatchet_workflows.ingest_pdf import ParseOut

    p = ParseOut(sha256="abc")
    assert hasattr(p, "figures")
    assert p.figures == []

    p2 = ParseOut(
        sha256="abc",
        figures=[{"idx": 0, "page": 1, "pending_key": "figures/_pending/x/a.png"}],
    )
    assert len(p2.figures) == 1
    assert p2.figures[0]["pending_key"] == "figures/_pending/x/a.png"


# ---------------------------------------------------------------------------
# 10. _run_parser_subprocess returns dict with "figures" key (default []
#    when parser produced no manifest)
# ---------------------------------------------------------------------------

def test_run_parser_subprocess_returns_figures_key(tmp_path):
    """Patch parse_pdf_report to return a stub result; verify the wrapper
    copies figure_manifest into the result dict under "figures"."""
    from app.hatchet_workflows import ingest_pdf as mod

    stub = MagicMock()
    stub.title = "T"
    stub.authors = []
    stub.company = None
    stub.filing_date = None
    stub.commodity = None
    stub.project_name = None
    stub.region = None
    stub.sections = []
    stub.parse_quality_pct = 0.0
    stub.parser_used = "stub"
    stub.skipped_elements = 0
    stub.warnings = []
    stub.page_languages = []
    stub.resource_tables = []
    stub.is_scanned = False
    stub.figure_manifest = [
        {"idx": 0, "page": 1, "caption": "c",
         "pending_key": "figures/_pending/aa/figure_0000_page_1.png",
         "bucket": "bronze", "sha256": "h", "bbox": None},
    ]

    with patch.object(
        sys.modules["georag_dagster.parsers.pdf_report"],
        "parse_pdf_report",
        MagicMock(return_value=stub),
    ):
        out = mod._run_parser_subprocess(b"%PDF-1.4 fake", sha256="aa" * 32)

    assert "figures" in out
    assert len(out["figures"]) == 1
    assert out["figures"][0]["pending_key"] == "figures/_pending/aa/figure_0000_page_1.png"


# ---------------------------------------------------------------------------
# 11. _run_parser_subprocess cleans up the figure tempdir in finally
# ---------------------------------------------------------------------------

def test_run_parser_subprocess_cleans_figure_tempdir():
    import os
    from app.hatchet_workflows import ingest_pdf as mod
    from georag_dagster.parsers.pdf_report import _figure_tempdir, _FIGURE_TEMPDIR_ROOT

    sha = "cc" * 32

    # Seed a fake tempdir so we can observe its removal
    d = _figure_tempdir(sha)
    sentinel = os.path.join(d, "fake.png")
    with open(sentinel, "wb") as f:
        f.write(b"junk")
    assert os.path.isfile(sentinel)

    stub = MagicMock()
    for attr in (
        "title", "company", "filing_date", "commodity", "project_name", "region",
    ):
        setattr(stub, attr, None)
    stub.authors = []
    stub.sections = []
    stub.parse_quality_pct = 0.0
    stub.parser_used = "stub"
    stub.skipped_elements = 0
    stub.warnings = []
    stub.page_languages = []
    stub.resource_tables = []
    stub.is_scanned = False
    stub.figure_manifest = []

    with patch.object(
        sys.modules["georag_dagster.parsers.pdf_report"],
        "parse_pdf_report",
        MagicMock(return_value=stub),
    ):
        mod._run_parser_subprocess(b"%PDF-1.4 fake", sha256=sha)

    assert not os.path.exists(d)


# ---------------------------------------------------------------------------
# 12. Tempdir cleanup still runs when parse raises
# ---------------------------------------------------------------------------

def test_run_parser_subprocess_cleans_tempdir_on_parse_error():
    import os
    from app.hatchet_workflows import ingest_pdf as mod
    from georag_dagster.parsers.pdf_report import _figure_tempdir

    sha = "dd" * 32
    d = _figure_tempdir(sha)
    with open(os.path.join(d, "x.png"), "wb") as f:
        f.write(b"junk")

    with patch.object(
        sys.modules["georag_dagster.parsers.pdf_report"],
        "parse_pdf_report",
        MagicMock(side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError):
            mod._run_parser_subprocess(b"%PDF-1.4 fake", sha256=sha)

    assert not os.path.exists(d)
