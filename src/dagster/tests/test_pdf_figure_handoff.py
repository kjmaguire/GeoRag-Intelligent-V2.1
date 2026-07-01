"""Phase 1 (2026-05-22) — figure subprocess handoff tests.

The previous _DOCLING_FIGURE_CACHE + _extract_docling_figures pair silently
returned [] once parse moved into a subprocess (cache lived in the parse
worker's memory, persist read it in the parent where it was always empty).
The fix moves figure upload inline into _parse_with_docling and threads
the manifest through ReportParseResult → ParseOut → persist.

These tests cover the parser side: manifest shape, no-figures cases,
graceful degradation when boto3 / S3 credentials are unavailable, and
ReportParseResult propagation.

No real docling install required — the relevant docling symbols are
patched. No real S3 either — boto3.client is patched.

Run with:
    pytest src/dagster/tests/test_pdf_figure_handoff.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Inject a fake `boto3` + `botocore.config` into sys.modules so the parser's
# `import boto3` inside _parse_with_docling resolves on dev hosts that don't
# have boto3 installed. The container image installs boto3 for real; this
# stub is purely for host-side unit testing.
_fake_boto3 = MagicMock()
_fake_botocore = MagicMock()
_fake_botocore_config = MagicMock()
sys.modules.setdefault("boto3", _fake_boto3)
sys.modules.setdefault("botocore", _fake_botocore)
sys.modules.setdefault("botocore.config", _fake_botocore_config)

from georag_dagster.parsers.pdf_report import (  # noqa: E402
    ReportParseResult,
    ReportSection,
    _figure_tempdir,
    _parse_with_docling,
)


# ---------------------------------------------------------------------------
# Fixtures: synthetic docling "Document" with pictures + a mocked S3 client
# ---------------------------------------------------------------------------


def _fake_prov(page_no: int, bbox=(0.0, 0.0, 10.0, 10.0)):
    """Build a minimal prov[0] mock with .page_no + .bbox."""
    prov = MagicMock()
    prov.page_no = page_no
    bx = MagicMock()
    bx.l, bx.t, bx.r, bx.b = bbox
    prov.bbox = bx
    return [prov]


def _fake_picture(page_no: int, caption: str, has_image: bool = True):
    pic = MagicMock()
    pic.prov = _fake_prov(page_no)
    pic.caption_text = MagicMock(return_value=caption)
    if has_image:
        pil_img = MagicMock()

        def _save(buf, format="PNG", optimize=True):  # noqa: A002
            buf.write(b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes")

        pil_img.save = _save
        pic.get_image = MagicMock(return_value=pil_img)
    else:
        pic.get_image = MagicMock(return_value=None)
    return pic


def _fake_doc(pictures, tables=None, texts=None):
    doc = MagicMock()
    doc.pictures = pictures
    doc.tables = tables or []
    doc.texts = texts or []
    doc.export_to_markdown = MagicMock(return_value="# Hello\n\nbody text")
    return doc


@pytest.fixture
def fake_s3():
    """Patched boto3.client returning a MagicMock recording put_object calls.

    Replaces the .client attribute on the stub boto3 module injected at
    import time. Reset between tests so call_count starts clean.
    """
    client = MagicMock()
    client.put_object = MagicMock(return_value={"ETag": "abc"})
    factory = MagicMock(return_value=client)
    prev = getattr(sys.modules["boto3"], "client", None)
    sys.modules["boto3"].client = factory
    try:
        yield client, factory
    finally:
        if prev is not None:
            sys.modules["boto3"].client = prev


@pytest.fixture
def docling_env(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:8333")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("S3_BUCKET_BRONZE", "bronze")


@pytest.fixture
def patched_docling():
    """Patch docling.document_converter so _parse_with_docling can run without
    the heavy docling install. Caller supplies pictures via the returned helper.
    """

    fake_result = MagicMock()
    fake_result.document = None  # set per test via the helper below

    def set_pictures(pictures, tables=None, texts=None):
        fake_result.document = _fake_doc(pictures, tables=tables, texts=texts)

    converter = MagicMock()
    converter.convert = MagicMock(return_value=fake_result)
    DocumentConverter = MagicMock(return_value=converter)

    with patch.dict(
        "sys.modules",
        {
            "docling.document_converter": MagicMock(
                DocumentConverter=DocumentConverter,
                PdfFormatOption=MagicMock(),
            ),
            "docling.datamodel.base_models": MagicMock(InputFormat=MagicMock()),
            "docling.datamodel.pipeline_options": MagicMock(
                AcceleratorDevice=MagicMock(CUDA="cuda"),
                AcceleratorOptions=MagicMock(),
                PdfPipelineOptions=MagicMock,
            ),
        },
    ):
        yield set_pictures


# ---------------------------------------------------------------------------
# 1. helper: _figure_tempdir creates per-sha dir
# ---------------------------------------------------------------------------

def test_figure_tempdir_creates_per_sha_dir(tmp_path, monkeypatch):
    # Re-point the constant for isolation
    sha = "deadbeef" * 8
    d = _figure_tempdir(sha)
    assert d.endswith(f"/{sha}")
    assert os.path.isdir(d)
    # Idempotent — calling again returns same path, doesn't crash
    d2 = _figure_tempdir(sha)
    assert d == d2


# ---------------------------------------------------------------------------
# 2. _parse_with_docling returns 8-tuple including empty figure_manifest
#    when pdf_sha256 is None (subprocess invoked without sha)
# ---------------------------------------------------------------------------

def test_parse_with_docling_returns_empty_manifest_when_sha_none(
    patched_docling, docling_env, fake_s3
):
    patched_docling(pictures=[_fake_picture(1, "Caption A")])

    out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256=None)

    assert isinstance(out, tuple) and len(out) == 8
    figure_manifest = out[7]
    assert figure_manifest == []
    # No S3 calls when sha is None
    client, _ = fake_s3
    client.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# 3. _parse_with_docling builds a manifest entry per picture, uploads to
#    figures/_pending/{sha}/...
# ---------------------------------------------------------------------------

def test_parse_with_docling_builds_manifest_and_uploads_to_pending(
    patched_docling, docling_env, fake_s3
):
    pictures = [
        _fake_picture(3, "Caption A"),
        _fake_picture(7, "Caption B"),
    ]
    patched_docling(pictures=pictures)

    sha = "a" * 64
    out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256=sha)
    manifest = out[7]

    assert len(manifest) == 2
    assert manifest[0]["idx"] == 0
    assert manifest[0]["page"] == 3
    assert manifest[0]["caption"] == "Caption A"
    assert manifest[0]["bucket"] == "bronze"
    assert manifest[0]["pending_key"] == (
        f"figures/_pending/{sha}/figure_0000_page_3.png"
    )
    assert manifest[0]["sha256"]  # populated when img bytes present

    assert manifest[1]["pending_key"] == (
        f"figures/_pending/{sha}/figure_0001_page_7.png"
    )

    client, _ = fake_s3
    assert client.put_object.call_count == 2
    first_call = client.put_object.call_args_list[0]
    assert first_call.kwargs["Bucket"] == "bronze"
    assert first_call.kwargs["Key"] == manifest[0]["pending_key"]
    assert first_call.kwargs["ContentType"] == "image/png"


# ---------------------------------------------------------------------------
# 4. Missing S3 creds → empty manifest, no exception
# ---------------------------------------------------------------------------

def test_parse_with_docling_skips_upload_when_creds_missing(
    patched_docling, fake_s3, monkeypatch
):
    # Wipe creds
    for k in (
        "S3_ENDPOINT_URL",
        "MINIO_ENDPOINT",
        "AWS_ACCESS_KEY_ID",
        "MINIO_ROOT_USER",
        "AWS_SECRET_ACCESS_KEY",
        "MINIO_ROOT_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)

    patched_docling(pictures=[_fake_picture(1, "Caption")])
    out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256="b" * 64)

    assert out[7] == []
    client, _ = fake_s3
    client.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Picture with no image bytes still appears in manifest with pending_key=None
#    (so persist can still record the caption section)
# ---------------------------------------------------------------------------

def test_parse_with_docling_handles_picture_without_image(
    patched_docling, docling_env, fake_s3
):
    patched_docling(pictures=[_fake_picture(2, "Caption only", has_image=False)])

    out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256="c" * 64)
    manifest = out[7]

    assert len(manifest) == 1
    assert manifest[0]["caption"] == "Caption only"
    assert manifest[0]["pending_key"] is None
    assert manifest[0]["sha256"] is None
    client, _ = fake_s3
    client.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Picture without prov (no page_no) is skipped silently
# ---------------------------------------------------------------------------

def test_parse_with_docling_skips_picture_without_page(
    patched_docling, docling_env, fake_s3
):
    pic = _fake_picture(5, "Has page")
    no_page = _fake_picture(99, "No page")
    no_page.prov = []
    patched_docling(pictures=[no_page, pic])

    out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256="d" * 64)
    manifest = out[7]

    # Only the one with prov survives
    assert len(manifest) == 1
    assert manifest[0]["page"] == 5


# ---------------------------------------------------------------------------
# 7. put_object failure → pending_key set to None for that entry, others succeed
# ---------------------------------------------------------------------------

def test_parse_with_docling_handles_upload_failure(
    patched_docling, docling_env
):
    pictures = [_fake_picture(1, "A"), _fake_picture(2, "B")]

    client = MagicMock()
    client.put_object = MagicMock(
        side_effect=[Exception("network down"), {"ETag": "ok"}]
    )
    prev = sys.modules["boto3"].client
    sys.modules["boto3"].client = MagicMock(return_value=client)
    try:
        patched_docling(pictures=pictures)
        out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256="e" * 64)
    finally:
        sys.modules["boto3"].client = prev

    manifest = out[7]
    assert len(manifest) == 2
    assert manifest[0]["pending_key"] is None  # first upload failed
    assert manifest[1]["pending_key"].endswith("figure_0001_page_2.png")


# ---------------------------------------------------------------------------
# 8. No pictures at all → empty manifest, no S3 client constructed
# ---------------------------------------------------------------------------

def test_parse_with_docling_no_pictures_no_s3(
    patched_docling, docling_env, fake_s3
):
    patched_docling(pictures=[])
    out = _parse_with_docling("/tmp/fake.pdf", pdf_sha256="f" * 64)

    assert out[7] == []
    _, factory = fake_s3
    # boto3.client should NOT be called when there are no pictures
    factory.assert_not_called()


# ---------------------------------------------------------------------------
# 9. ReportParseResult exposes figure_manifest as a default-empty field
# ---------------------------------------------------------------------------

def test_report_parse_result_has_figure_manifest_default_empty():
    r = ReportParseResult(
        title="t",
        authors=[],
        company=None,
        filing_date=None,
        commodity=None,
        project_name=None,
        region=None,
        sections=[ReportSection(section_number="1", section_title="S", text="x")],
        parse_quality_pct=0.0,
    )
    assert hasattr(r, "figure_manifest")
    assert r.figure_manifest == []


def test_report_parse_result_accepts_figure_manifest():
    items = [{"idx": 0, "page": 1, "caption": "c", "pending_key": "figures/_pending/x/foo.png"}]
    r = ReportParseResult(
        title=None, authors=[], company=None, filing_date=None,
        commodity=None, project_name=None, region=None,
        sections=[], parse_quality_pct=0.0,
        figure_manifest=items,
    )
    assert r.figure_manifest == items


# ---------------------------------------------------------------------------
# 10. The legacy _extract_docling_figures + _DOCLING_FIGURE_CACHE symbols
#     are gone — guards against accidental re-introduction
# ---------------------------------------------------------------------------

def test_legacy_cache_symbols_removed():
    import georag_dagster.parsers.pdf_report as mod

    assert not hasattr(mod, "_DOCLING_FIGURE_CACHE")
    assert not hasattr(mod, "_DOCLING_FIGURE_CACHE_MAX")
    assert not hasattr(mod, "_pdf_sha256_for_cache")
    assert not hasattr(mod, "_extract_docling_figures")
    # And the new helper is exposed
    assert hasattr(mod, "_FIGURE_TEMPDIR_ROOT")
    assert hasattr(mod, "_figure_tempdir")
