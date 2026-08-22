"""The PDF ingester stamped "uranium" on every report row it created.

`pdf_ingester._get_or_create_document` carried its Wyoming Cameco / WSGS
origin as a literal in the middle of the `silver.reports` VALUES list:

    VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5,
            'uranium', $6, $7, 'pdfminer.six',

There was no parameter to override it — unlike `company` and `region`,
which are already caller-supplied on the same call. Every PDF ingested
through `cluster_runner` therefore claimed uranium regardless of what the
report was actually about.

`silver.reports.commodity` is nullable (varchar 50, see
2026_04_09_180800_create_reports_table.php) and this ingester extracts no
metadata at all — it reads page text and chunks it, nothing more — so
NULL, "the ingester did not determine one", is the only honest value.
Third in the same family, same reasoning:

  - `xlsx_ingester` — silver.reports.commodity (fixed 2026-08-21)
  - `las_ingester` + `cluster_runner` — silver.projects.commodity
    (fixed 2026-08-21, see test_las_project_commodity.py)

Downstream is already NULL-safe: the Qdrant payload builders coalesce
(`index_document_passages.py` `COALESCE(r.commodity, '')`,
`index_reports.py` `row.get("commodity") or ""`) and the Foundry surfaces
cast through `?? ''`.

`parser_used` is deliberately NOT parameterised here. Unlike xlsx — where
the literal would have had to cover both openpyxl and csv-text — this
module has exactly one extraction path, `_extract_text_pages`, which
imports `pdfminer.high_level` unconditionally and has no OCR fallback
(scanned PDFs return skipped_reason="empty_or_scanned_pdf_no_native_text").
'pdfminer.six' is therefore accurate, and the last class below pins that
so the label cannot drift away from the code the way "fitz" did in
`pdf_report._parse_with_fitz`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_DOC = "f5000000-0000-0000-0000-0000000000e0"

# Bind order of the silver.reports INSERT in pdf_ingester:
# project_id, workspace_id, title, company, region, source_sha256,
# is_scanned, page_count — plus commodity, appended by this fix.
_COMMODITY_ARG = 8


class _NullTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingConn:
    """Records the bind args and SQL of every INSERT INTO silver.reports.

    Permissive everywhere else — this suite only cares about what lands in
    the report row's commodity column.
    """

    def __init__(self, *, existing_report: str | None = None) -> None:
        self.existing_report = existing_report
        self.report_args: tuple | None = None
        self.report_sql: str | None = None
        self.report_inserts = 0

    def is_in_transaction(self) -> bool:
        # See the note in test_entity_resolver.py: bind_workspace_scope
        # now refuses SET LOCAL outside a transaction.
        return getattr(self, "_in_tx", False)

    def transaction(self):
        conn = self

        class _TrackedTx:
            async def __aenter__(self):
                conn._in_tx = True
                return None

            async def __aexit__(self, *exc):
                conn._in_tx = False
                return False

        return _TrackedTx()

    async def execute(self, sql: str, *args):
        return "SET"

    async def fetchval(self, sql: str, *args):
        return _PJ

    async def fetchrow(self, sql: str, *args):
        flat = " ".join(sql.split())
        if "INSERT INTO silver.reports" in flat:
            self.report_inserts += 1
            self.report_args = args
            self.report_sql = flat
            return {"report_id": _DOC}
        if "FROM silver.reports" in flat:
            if self.existing_report:
                return {"report_id": self.existing_report}
            return None
        if "INSERT INTO silver.document_passages" in flat:
            return {"passage_id": "aa000000-0000-0000-0000-0000000000ff"}
        if "INSERT INTO silver.projects" in flat:
            return {"project_id": _PJ}
        if flat.startswith("SELECT project_id"):
            return None
        return None


def _text_pdf(path: Path, *, body: str) -> None:
    """Write a minimal single-page PDF with a real, extractable text layer.

    Hand-built rather than mocked: `ingest_pdf_file` runs pdfminer for real,
    and a stub that skipped extraction would not prove the INSERT is reached.
    """
    stream_parts = ["BT", "/F1 12 Tf", "14 TL", "50 750 Td"]
    for line in body.split("\n"):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_parts.append("(" + escaped + ") Tj")
        stream_parts.append("T*")
    stream_parts.append("ET")
    stream = "\n".join(stream_parts).encode("ascii")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode() + b"\n%%EOF\n"
    )
    path.write_bytes(bytes(out))


# Comfortably over _MIN_CHUNK * 2 (200 chars) so the ingester does not bail
# with "empty_or_scanned_pdf_no_native_text", and unmistakably not uranium.
_GOLD_BODY = "\n".join([
    "ACME GOLD CORP - RED LAKE PROPERTY",
    "Technical Report on the Red Lake gold project, Kenora District, Ontario.",
    "Drilling intersected quartz-carbonate veins hosting visible gold within",
    "the Balmer assemblage mafic volcanic rocks. Assay results returned up to",
    "42.1 grams per tonne gold over 3.2 metres in hole RL-12-004.",
    "No radioactive mineralisation of any kind was encountered on the property.",
])


class TestTheReportRowIsHonest:
    @pytest.mark.asyncio
    async def test_the_commodity_is_not_stamped_uranium(self) -> None:
        """A gold report's row must not claim uranium."""
        from app.services.ingest.pdf_ingester import _get_or_create_document

        conn = _RecordingConn()
        await _get_or_create_document(
            conn,
            file_path="/data/red-lake.pdf",
            title="ACME GOLD - Red Lake Technical Report",
            project_id=_PJ,
            workspace_id=_WS,
            source_sha256="0" * 64,
            company="ACME GOLD CORP",
            region="KENORA, ON",
            page_count=42,
        )

        assert conn.report_args is not None
        assert "uranium" not in [str(a).lower() for a in conn.report_args]
        assert conn.report_sql is not None
        assert "uranium" not in conn.report_sql.lower()

    @pytest.mark.asyncio
    async def test_an_unstated_commodity_lands_as_null_not_a_guess(self) -> None:
        """The column is nullable; NULL is how "we did not determine one" is
        spelled. This ingester extracts no metadata, so it never has one of
        its own to offer."""
        from app.services.ingest.pdf_ingester import _get_or_create_document

        conn = _RecordingConn()
        await _get_or_create_document(
            conn,
            file_path="/data/red-lake.pdf",
            title="ACME GOLD - Red Lake Technical Report",
            project_id=_PJ,
            workspace_id=_WS,
            source_sha256="0" * 64,
        )

        assert conn.report_args[_COMMODITY_ARG] is None

    @pytest.mark.asyncio
    async def test_a_caller_that_knows_the_commodity_is_still_honoured(self) -> None:
        """Removing the literal must not remove the ability to state one."""
        from app.services.ingest.pdf_ingester import _get_or_create_document

        conn = _RecordingConn()
        await _get_or_create_document(
            conn,
            file_path="/data/red-lake.pdf",
            title="ACME GOLD - Red Lake Technical Report",
            project_id=_PJ,
            workspace_id=_WS,
            source_sha256="0" * 64,
            commodity="gold",
        )

        assert conn.report_args[_COMMODITY_ARG] == "gold"

    @pytest.mark.asyncio
    async def test_the_dedupe_short_circuit_writes_nothing(self) -> None:
        """A sha already in silver.reports returns early — no INSERT, so no
        commodity is written or overwritten either way."""
        from app.services.ingest.pdf_ingester import _get_or_create_document

        conn = _RecordingConn(existing_report=_DOC)
        got = await _get_or_create_document(
            conn,
            file_path="/data/red-lake.pdf",
            title="ACME GOLD - Red Lake Technical Report",
            project_id=_PJ,
            workspace_id=_WS,
            source_sha256="0" * 64,
            commodity="gold",
        )

        assert got == _DOC
        assert conn.report_inserts == 0


class TestTheFullIngestPath:
    @pytest.mark.asyncio
    async def test_a_real_gold_pdf_does_not_become_a_uranium_report(
        self, tmp_path,
    ) -> None:
        """End to end through pdfminer: the literal was unreachable by any
        caller, so only exercising `ingest_pdf_file` proves it is gone from
        the statement that actually fires."""
        from app.services.ingest.pdf_ingester import ingest_pdf_file

        path = tmp_path / "red-lake.pdf"
        _text_pdf(path, body=_GOLD_BODY)
        conn = _RecordingConn()

        result = await ingest_pdf_file(
            conn, str(path), workspace_id=_WS, project_id=_PJ,
        )

        assert not result.skipped, result.skipped_reason
        assert conn.report_inserts == 1
        assert "uranium" not in [str(a).lower() for a in conn.report_args]
        assert conn.report_args[_COMMODITY_ARG] is None

    @pytest.mark.asyncio
    async def test_ingest_pdf_file_forwards_a_caller_supplied_commodity(
        self, tmp_path,
    ) -> None:
        """The parameter has to be reachable from the public entry point, not
        just from the private helper."""
        from app.services.ingest.pdf_ingester import ingest_pdf_file

        path = tmp_path / "red-lake.pdf"
        _text_pdf(path, body=_GOLD_BODY)
        conn = _RecordingConn()

        result = await ingest_pdf_file(
            conn, str(path), workspace_id=_WS, project_id=_PJ, commodity="gold",
        )

        assert not result.skipped, result.skipped_reason
        assert conn.report_args[_COMMODITY_ARG] == "gold"


class TestTheClusterRunnerForwardsItsCommodity:
    @pytest.mark.asyncio
    async def test_the_pdf_report_inherits_the_clusters_commodity(
        self, tmp_path,
    ) -> None:
        """`ingest_cluster` already takes `project_commodity` for the stub
        project row. The PDFs in that cluster belong to the same project, so
        the same value is the truthful one for their report rows — and it is
        the only commodity any caller of this ingester actually holds."""
        from app.services.ingest.cluster_runner import ingest_cluster

        _text_pdf(tmp_path / "report.pdf", body=_GOLD_BODY)
        conn = _RecordingConn()

        await ingest_cluster(
            str(tmp_path),
            workspace_id=_WS,
            conn=conn,
            project_name="ACME GOLD Red Lake",
            project_slug="acme-gold-red-lake",
            project_company="ACME GOLD",
            project_region="KENORA, ON",
            project_commodity="gold",
        )

        assert conn.report_inserts == 1
        assert conn.report_args[_COMMODITY_ARG] == "gold"

    @pytest.mark.asyncio
    async def test_an_unstated_cluster_commodity_stays_null_downstream(
        self, tmp_path,
    ) -> None:
        """The default path — no commodity known anywhere — must not
        reintroduce a guess at either the project or the report row."""
        from app.services.ingest.cluster_runner import ingest_cluster

        _text_pdf(tmp_path / "report.pdf", body=_GOLD_BODY)
        conn = _RecordingConn()

        await ingest_cluster(
            str(tmp_path),
            workspace_id=_WS,
            conn=conn,
            project_name="ACME GOLD Red Lake",
            project_slug="acme-gold-red-lake",
            project_company="ACME GOLD",
            project_region="KENORA, ON",
        )

        assert conn.report_inserts == 1
        assert conn.report_args[_COMMODITY_ARG] is None


class TestParserUsedStillMatchesTheParser:
    def test_this_module_has_exactly_one_extraction_engine(self) -> None:
        """`parser_used` is hardcoded to 'pdfminer.six' in the same INSERT.
        That is accurate *because* there is one engine and no OCR fallback —
        so pin both halves of that claim together. If someone adds a second
        extraction path here, this fails and the label has to become a
        parameter, the way xlsx_ingester's did for openpyxl vs csv-text.

        The cautionary tale is `pdf_report._parse_with_fitz`, whose 'fitz'
        label outlived PyMuPDF's removal by more than a year.
        """
        src = (
            Path(__file__).resolve().parents[1]
            / "app" / "services" / "ingest" / "pdf_ingester.py"
        ).read_text(encoding="utf-8")

        assert "'pdfminer.six'" in src
        assert "pdfminer.high_level" in src
        for other in ("pypdfium2", "pdfplumber", "import fitz", "tesseract",
                      "document_intelligence"):
            assert other not in src, (
                other + " appeared in pdf_ingester — parser_used='pdfminer.six'"
                " is now a guess and must become a parameter"
            )

    @pytest.mark.asyncio
    async def test_a_scanned_pdf_is_skipped_rather_than_relabelled(
        self, tmp_path,
    ) -> None:
        """No OCR fallback exists, so no row is ever written with a parser
        that isn't pdfminer. A text-free PDF bails before the INSERT."""
        from app.services.ingest.pdf_ingester import ingest_pdf_file

        path = tmp_path / "scanned.pdf"
        _text_pdf(path, body="pg 1")  # under _MIN_CHUNK * 2

        conn = _RecordingConn()
        result = await ingest_pdf_file(conn, str(path), workspace_id=_WS)

        assert result.skipped
        assert result.skipped_reason == "empty_or_scanned_pdf_no_native_text"
        assert conn.report_inserts == 0
