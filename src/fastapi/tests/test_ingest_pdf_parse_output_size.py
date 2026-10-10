"""ingest_pdf.parse must not return an output the engine cannot carry.

hatchet_sdk caps one gRPC message at 4 MiB by default, and a task's output
travels to the engine and on to every child task as one. parse returned the whole
document (sections, resource tables, ...) as that output. For a long report the
send failed at the END of a 4-hour task that had already paid for every OCR page,
and retries=1 then re-ran the parse and re-paid it for the same failure.

Below the inline limit the output is returned as it always was; above it the heavy
fields are packed (gzip + base64) and persist unpacks them; if even the packed
output would not fit, the parse fails once, non-retryably, with the reason.
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
from hatchet_sdk import NonRetryableException

from app.hatchet_workflows import ingest_pdf as ipdf

SDK_GRPC_LIMIT = 4 * 1024 * 1024  # hatchet_sdk.config.grpc_max_{send,recv}_message_length default

_WORDS = ("tonnes", "grade", "inferred", "indicated", "drill", "hole", "assay", "uranium",
          "zone", "mineralization", "lithology", "section", "resource", "estimate")


def _prose(n_chars: int, seed: int = 7) -> str:
    rnd = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < n_chars:
        w = rnd.choice(_WORDS) if rnd.random() < 0.9 else f"{rnd.random():.4f}"
        out.append(w)
        size += len(w) + 1
    return " ".join(out)


def _out(*, section_chars: int, n_sections: int = 40) -> ipdf.ParseOut:
    per = section_chars // n_sections
    return ipdf.ParseOut(
        sha256="a" * 64,
        title="NI 43-101 Technical Report",
        parser_used="pdfplumber",
        sections=[{"heading": f"Item {i}", "text": _prose(per, seed=i), "page_start": i} for i in range(n_sections)],
        resource_tables=[{"page": 5, "rows": [["Zone", "Tonnes", "Grade"], ["A", "1000", "0.4"]]}],
        warnings=[{"code": "low_text", "message": "page 12 has little text"}],
        page_languages=["en"] * 3,
        parse_quality_pct=87.5,
    )


def test_a_typical_output_is_returned_exactly_as_before() -> None:
    out = _out(section_chars=300_000)
    assert ipdf._serialised_size(out) < ipdf.PARSE_OUTPUT_INLINE_MAX_BYTES

    packed = ipdf._pack_parse_output(out)

    assert packed is out
    assert packed.heavy_gz_b64 is None and packed.sections


def test_an_output_over_the_grpc_limit_is_packed_to_fit_and_survives_the_round_trip() -> None:
    out = _out(section_chars=6_000_000)
    assert ipdf._serialised_size(out) > SDK_GRPC_LIMIT, "the premise: this would not have fit"

    packed = ipdf._pack_parse_output(out)

    assert ipdf._serialised_size(packed) < SDK_GRPC_LIMIT
    assert packed.heavy_gz_b64
    # The bulk moved; the small fields (and parser_used, which embed_verify reads) did not.
    assert packed.sections == [] and packed.resource_tables == [] and packed.warnings == []
    assert (packed.sha256, packed.title, packed.parser_used, packed.parse_quality_pct) == (
        out.sha256, out.title, out.parser_used, out.parse_quality_pct,
    )

    restored = ipdf._unpack_parse_output(packed.model_dump())

    expected = out.model_dump()
    assert restored["heavy_gz_b64"] is None
    for key, value in expected.items():
        if key != "heavy_gz_b64":
            assert restored[key] == value, key


def test_unpacking_an_output_that_was_never_packed_changes_nothing() -> None:
    parsed = _out(section_chars=10_000).model_dump()
    assert ipdf._unpack_parse_output(parsed) is parsed


def test_an_output_that_cannot_fit_even_packed_fails_once_and_says_why() -> None:
    rnd = random.Random(1)
    noise = "".join(rnd.choices("0123456789abcdef", k=14_000_000))  # incompressible enough
    out = ipdf.ParseOut(sha256="b" * 64, parser_used="pdfplumber", sections=[{"text": noise}])

    with pytest.raises(NonRetryableException, match="PARSE_OUTPUT_MAX_BYTES"):
        ipdf._pack_parse_output(out)


def test_the_limits_are_below_the_sdk_default() -> None:
    assert ipdf.PARSE_OUTPUT_INLINE_MAX_BYTES < ipdf.PARSE_OUTPUT_MAX_BYTES < SDK_GRPC_LIMIT


class _Stop(Exception):
    pass


async def test_persist_reads_the_parse_output_through_the_unpacker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consumer half: persist must call the unpacker before it reads a section."""
    seen: list[dict] = []

    def _spy(parsed: dict) -> dict:
        seen.append(parsed)
        raise _Stop

    monkeypatch.setattr(ipdf, "_unpack_parse_output", _spy)
    packed = ipdf._pack_parse_output(_out(section_chars=6_000_000)).model_dump()
    ctx = SimpleNamespace(
        task_output=lambda task: {"sha256": "a" * 64, "valid": True} if task is ipdf.preflight else packed,
    )
    inp = ipdf.IngestPdfInput(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="b0000000-0000-0000-0000-000000000002",
        minio_key="reports/p/x.pdf", file_size=1, correlation_token="t",
    )

    with pytest.raises(_Stop):
        await ipdf._persist_body(inp, ctx)

    assert seen and seen[0]["heavy_gz_b64"] == packed["heavy_gz_b64"] and seen[0]["sections"] == []
