"""The PDF ingester's sha dedupe had no project predicate.

`pdf_ingester._get_or_create_document` looked up an existing report by file
hash alone:

    SELECT report_id::text AS report_id
      FROM silver.reports
     WHERE source_file_sha256 = $1
     LIMIT 1

So the same PDF uploaded into a SECOND project found the FIRST project's
report and wrote its passages under it — invisible in the project the user
actually uploaded to, and attached to one they may not even be a member of.
Identical defect, identical fix, as `xlsx_ingester.land_sheets_as_text`
(2026-08-21); a shared NI 43-101 or a regional survey PDF landing in two
projects is not an exotic way for a report sha to collide.

Cross-WORKSPACE was never the hole here: `silver.reports` carries
workspace-scoped RLS (database/raw/phase0/96-rls-tenant-isolation-block1.sql)
and `cluster_runner._set_rls_gucs` binds `app.workspace_id` before each pass.
What leaked was cross-PROJECT inside one workspace.

NULL project_id — where this DIVERGES from xlsx
-----------------------------------------------
`ingest_pdf_file` takes `project_id: str | None = None`, and the None path
is reachable: `cluster_runner` resolves the project by slug and then by two
fallback lookups (lines ~263-272) and can come out with None, after which
the PDF pass runs anyway — unlike the .log pass, it has no `if not
project_id: continue` guard.

xlsx spells its predicate `AND project_id = $2::uuid`. Under Postgres'
three-valued logic `project_id = NULL` is UNKNOWN, never true, so that
statement matches nothing when the caller has no project — and an unscoped
ingest would mint a fresh report row on every single call, silently
duplicating every passage under a new document_id. That trades a
cross-project leak for a broken idempotency contract, and this helper's
docstring promises idempotency in its first line.

`IS NOT DISTINCT FROM` keeps both halves: a bound project matches only its
own rows, and NULL matches NULL — i.e. an unscoped ingest dedupes against
other unscoped ingests and nothing else. A NULL project_id row belongs to
no project, so matching it leaks into no project. The idiom is already this
codebase's way of spelling "NULL is a scope, not an unknown" — see the RLS
policies in database/raw/phase0/95-rls-policies.sql and the audit hash chain
in 90-audit-hash-chain-trigger.sql, which use it for exactly that.

`xlsx_ingester` still uses `=` and therefore still duplicates on the
unscoped path. Out of scope here, flagged separately.
"""
from __future__ import annotations

import pytest

from tests.test_pdf_report_commodity import _text_pdf

_WS = "a0000000-0000-0000-0000-00000000feed"
_PROJECT_A = "b1000000-0000-0000-0000-0000000000a0"
_PROJECT_B = "b2000000-0000-0000-0000-0000000000b0"
_SEEDED_REPORT = "f5000000-0000-0000-0000-0000000000e0"
_SHA = "0" * 64

# Bind order of the silver.reports INSERT in pdf_ingester:
#   $1 project_id, $2 workspace_id, $3 title, $4 company, $5 region,
#   $6 source_sha256, $7 is_scanned, $8 page_count, $9 commodity
_INSERT_PROJECT_ARG = 0
_INSERT_SHA_ARG = 5

# Comfortably over _MIN_CHUNK * 2 (200 chars) so the ingester does not bail
# with "empty_or_scanned_pdf_no_native_text" before reaching any SQL.
_BODY = "\n".join([
    "ACME GOLD CORP - RED LAKE PROPERTY",
    "Technical Report on the Red Lake gold project, Kenora District, Ontario.",
    "Drilling intersected quartz-carbonate veins hosting visible gold within",
    "the Balmer assemblage mafic volcanic rocks. Assay results returned up to",
    "42.1 grams per tonne gold over 3.2 metres in hole RL-12-004.",
    "A shared regional survey PDF of exactly this kind lands in many projects.",
])


class _ReportsTable:
    """A stand-in for `silver.reports` that answers the dedupe SELECT the way
    Postgres would, rather than the way the test would like.

    `_RecordingConn` in test_pdf_report_commodity returns its seeded row for
    any `FROM silver.reports` statement, because that suite only cares what
    lands in the INSERT. This fix is entirely about WHICH rows the lookup is
    allowed to see, so a fake that ignores the predicate could not tell the
    broken statement from the fixed one. This one applies the predicate the
    statement actually asks for, three-valued logic included: `col = NULL`
    is UNKNOWN and matches nothing, `col IS NOT DISTINCT FROM NULL` matches
    NULL. That is what makes the unscoped-ingest test below meaningful in
    either direction.

    Permissive everywhere else — passages are recorded, not validated.
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: list[dict] = list(rows or [])
        self.report_args: tuple | None = None
        self.report_sql: str | None = None
        self.report_inserts = 0
        self.select_args: tuple | None = None
        self.select_sql: str | None = None
        self.passage_document_ids: list[str] = []

    def is_in_transaction(self) -> bool:
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
        return None

    def _dedupe_lookup(self, flat: str, args: tuple) -> dict | None:
        hits = [r for r in self.rows if r["sha"] == args[0]]
        if "project_id" in flat:
            wanted = args[1] if len(args) > 1 else None
            if "IS NOT DISTINCT FROM" in flat:
                hits = [r for r in hits if r["project_id"] == wanted]
            else:
                # `col = NULL` yields UNKNOWN, so the row never qualifies.
                hits = [] if wanted is None else [
                    r for r in hits
                    if r["project_id"] is not None and r["project_id"] == wanted
                ]
        return {"report_id": hits[0]["report_id"]} if hits else None

    async def fetchrow(self, sql: str, *args):
        flat = " ".join(sql.split())
        if "INSERT INTO silver.reports" in flat:
            self.report_inserts += 1
            self.report_args = args
            self.report_sql = flat
            report_id = f"c0000000-0000-0000-0000-{self.report_inserts:012d}"
            self.rows.append({
                "report_id": report_id,
                "sha": args[_INSERT_SHA_ARG],
                "project_id": args[_INSERT_PROJECT_ARG],
            })
            return {"report_id": report_id}
        if "FROM silver.reports" in flat:
            self.select_args = args
            self.select_sql = flat
            return self._dedupe_lookup(flat, args)
        if "INSERT INTO silver.document_passages" in flat:
            self.passage_document_ids.append(args[0])
            return {"passage_id": "aa000000-0000-0000-0000-0000000000ff"}
        if "INSERT INTO silver.projects" in flat:
            return {"project_id": _PROJECT_A}
        return None


def _seeded(project_id: str | None) -> _ReportsTable:
    """One pre-existing report for `_SHA`, owned by `project_id`."""
    return _ReportsTable([
        {"report_id": _SEEDED_REPORT, "sha": _SHA, "project_id": project_id},
    ])


async def _get_or_create(conn, *, project_id: str | None) -> str:
    from app.services.ingest.pdf_ingester import _get_or_create_document

    return await _get_or_create_document(
        conn,
        file_path="/data/red-lake.pdf",
        title="ACME GOLD - Red Lake Technical Report",
        project_id=project_id,
        workspace_id=_WS,
        source_sha256=_SHA,
    )


class TestTheDedupeLookupIsScopedToTheProject:
    @pytest.mark.asyncio
    async def test_the_lookup_binds_the_project_id(self) -> None:
        """The statement has to ask about the project at all — with only the
        sha bound there is nothing to scope on."""
        conn = _ReportsTable()
        await _get_or_create(conn, project_id=_PROJECT_A)

        assert conn.select_sql is not None
        assert "project_id" in conn.select_sql
        assert conn.select_args is not None
        assert _PROJECT_A in conn.select_args

    @pytest.mark.asyncio
    async def test_a_second_project_does_not_adopt_the_first_projects_report(
        self,
    ) -> None:
        """The defect, stated directly: project B uploads the same PDF and
        must get its own report row, not project A's."""
        conn = _seeded(_PROJECT_A)

        document_id = await _get_or_create(conn, project_id=_PROJECT_B)

        assert document_id != _SEEDED_REPORT
        assert conn.report_inserts == 1
        assert conn.report_args[_INSERT_PROJECT_ARG] == _PROJECT_B

    @pytest.mark.asyncio
    async def test_the_same_project_re_uploading_still_dedupes(self) -> None:
        """Scoping the lookup must not cost the idempotency it was there for
        in the first place."""
        conn = _seeded(_PROJECT_A)

        document_id = await _get_or_create(conn, project_id=_PROJECT_A)

        assert document_id == _SEEDED_REPORT
        assert conn.report_inserts == 0


class TestAnUnscopedIngestStillDedupes:
    """`project_id=None` is reachable — see the module docstring — and is
    where this deliberately diverges from xlsx_ingester's `=`."""

    @pytest.mark.asyncio
    async def test_two_unscoped_ingests_of_one_file_share_a_report(self) -> None:
        """`= NULL` is UNKNOWN, so a plain equality predicate would mint a
        second report here and duplicate every passage under it."""
        conn = _ReportsTable()

        first = await _get_or_create(conn, project_id=None)
        second = await _get_or_create(conn, project_id=None)

        assert first == second
        assert conn.report_inserts == 1

    @pytest.mark.asyncio
    async def test_an_unscoped_ingest_does_not_adopt_a_projects_report(
        self,
    ) -> None:
        """NULL matching NULL must not widen into NULL matching anything."""
        conn = _seeded(_PROJECT_A)

        document_id = await _get_or_create(conn, project_id=None)

        assert document_id != _SEEDED_REPORT
        assert conn.report_inserts == 1
        assert conn.report_args[_INSERT_PROJECT_ARG] is None

    @pytest.mark.asyncio
    async def test_a_projects_ingest_does_not_adopt_an_unscoped_report(
        self,
    ) -> None:
        """And the same in reverse — a project-less row is not up for grabs."""
        conn = _seeded(None)

        document_id = await _get_or_create(conn, project_id=_PROJECT_A)

        assert document_id != _SEEDED_REPORT
        assert conn.report_inserts == 1
        assert conn.report_args[_INSERT_PROJECT_ARG] == _PROJECT_A


class TestTheFullIngestPath:
    @pytest.mark.asyncio
    async def test_one_pdf_in_two_projects_lands_two_reports(
        self, tmp_path,
    ) -> None:
        """End to end through pdfminer, byte-identical file both times. The
        consequence that made this worth fixing is where the PASSAGES go, so
        assert on those and not just on the report row."""
        from app.services.ingest.pdf_ingester import ingest_pdf_file

        path = tmp_path / "red-lake.pdf"
        _text_pdf(path, body=_BODY)
        conn = _ReportsTable()

        first = await ingest_pdf_file(
            conn, str(path), workspace_id=_WS, project_id=_PROJECT_A,
        )
        passages_after_first = len(conn.passage_document_ids)
        second = await ingest_pdf_file(
            conn, str(path), workspace_id=_WS, project_id=_PROJECT_B,
        )

        assert not first.skipped, first.skipped_reason
        assert not second.skipped, second.skipped_reason
        assert conn.report_inserts == 2
        assert first.document_id != second.document_id
        assert set(conn.passage_document_ids[:passages_after_first]) == {
            first.document_id,
        }
        assert set(conn.passage_document_ids[passages_after_first:]) == {
            second.document_id,
        }

    @pytest.mark.asyncio
    async def test_re_ingesting_into_the_same_project_reuses_the_report(
        self, tmp_path,
    ) -> None:
        """The passage insert is `ON CONFLICT DO NOTHING` on
        (document_id, revision_number, text_hash), which only dedupes when
        the second pass lands on the SAME document_id."""
        from app.services.ingest.pdf_ingester import ingest_pdf_file

        path = tmp_path / "red-lake.pdf"
        _text_pdf(path, body=_BODY)
        conn = _ReportsTable()

        first = await ingest_pdf_file(
            conn, str(path), workspace_id=_WS, project_id=_PROJECT_A,
        )
        second = await ingest_pdf_file(
            conn, str(path), workspace_id=_WS, project_id=_PROJECT_A,
        )

        assert conn.report_inserts == 1
        assert first.document_id == second.document_id
