"""``_extract_archive`` truncated over-size deliveries and reported success.

A spatial archive is an INTERDEPENDENT file set. A shapefile is a ``.shp``
plus a ``.dbf`` plus a ``.shx`` plus usually a ``.prj``, stored in whatever
order the zipping tool chose. The old extractor accumulated declared sizes
inside the member loop and ``break``-ed out when the running total crossed
2 GiB, with nothing but a log line — so a 3 GiB delivery lost whichever
members happened to sit past the cut-off, and if that was a ``.dbf`` the
layer that DID land had geometry and no attributes, or coordinates read in
the wrong CRS. The run then reported ``completed``.

Three separate things were measured against the live function before these
expectations were written:

* the cap tripped only after up to 2 GiB was already on disk;
* there was no entry-count cap at all (the sibling extractor in
  ``ingest_zip_archive`` has capped at 50,000 since the 2026-06-28 audit);
* the zip-slip guard was a bare string prefix, so a member named
  ``../_unzipped_evil/pwn.geojson`` really was written outside the
  extraction root.
"""
from __future__ import annotations

import ast
import json
import zipfile
from pathlib import Path

import pytest

from app.hatchet_workflows import ingest_spatial as sp

_GEOJSON = json.dumps({
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"n": 1},
        "geometry": {"type": "Point", "coordinates": [-108.7, 43.0]},
    }],
}).encode()


def _zip(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


class TestOverSizeArchivesAreRefusedNotTruncated:
    def test_an_over_cap_archive_raises(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sp, "_MAX_EXPANDED_BYTES", 1_000)
        archive = _zip(tmp_path / "delivery.zip", [
            ("faults.geojson", _GEOJSON),
            ("filler.dat", b"x" * 200_000),
            ("claims.geojson", _GEOJSON),
        ])

        with pytest.raises(ValueError, match="expands to"):
            sp._extract_archive(archive, tmp_path / "out")

    def test_nothing_is_written_before_the_refusal(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The cap used to be applied while writing.

        So refusing a 3 GiB archive first required putting 2 GiB of it on
        the worker's disk — the one case the cap exists for was the one
        case it could not help.
        """
        monkeypatch.setattr(sp, "_MAX_EXPANDED_BYTES", 1_000)
        dest = tmp_path / "out"
        archive = _zip(tmp_path / "delivery.zip", [
            ("faults.geojson", _GEOJSON),
            ("filler.dat", b"x" * 200_000),
        ])

        with pytest.raises(ValueError):
            sp._extract_archive(archive, dest)

        written = list(dest.rglob("*")) if dest.exists() else []
        assert written == [], f"partial extraction left {written}"

    def test_the_message_says_what_to_do_about_it(
        self, tmp_path, monkeypatch,
    ) -> None:
        """It reaches the user: the task's except clause passes str(exc)
        into mark_failed_by_run, which the Ingestion Runs page renders."""
        monkeypatch.setattr(sp, "_MAX_EXPANDED_BYTES", 1_000)
        archive = _zip(tmp_path / "d.zip", [("a.dat", b"x" * 2_000)])

        with pytest.raises(ValueError) as excinfo:
            sp._extract_archive(archive, tmp_path / "out")

        assert "Split the delivery" in str(excinfo.value)

    def test_an_archive_inside_the_cap_is_untouched(self, tmp_path) -> None:
        archive = _zip(tmp_path / "ok.zip", [
            ("faults.geojson", _GEOJSON),
            ("claims.geojson", _GEOJSON),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == [
            "claims.geojson", "faults.geojson",
        ]
        assert result.warnings == []


class TestEntryCountCap:
    def test_too_many_entries_is_refused(self, tmp_path, monkeypatch) -> None:
        """Parity with ingest_zip_archive.

        200,000 one-byte members pass a size cap comfortably and still
        spend the run's whole 2 h budget on inode churn.
        """
        monkeypatch.setattr(sp, "_MAX_ARCHIVE_ENTRIES", 5)
        archive = _zip(tmp_path / "many.zip", [
            (f"l{i}.geojson", _GEOJSON) for i in range(9)
        ])

        with pytest.raises(ValueError, match="entries"):
            sp._extract_archive(archive, tmp_path / "out")

    def test_directory_entries_do_not_count_against_the_cap(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A deeply nested but small delivery is not a zip bomb."""
        monkeypatch.setattr(sp, "_MAX_ARCHIVE_ENTRIES", 3)
        path = tmp_path / "nested.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a/", b"")
            zf.writestr("a/b/", b"")
            zf.writestr("a/b/c/", b"")
            zf.writestr("a/b/c/faults.geojson", _GEOJSON)

        result = sp._extract_archive(path, tmp_path / "out")

        assert [p.name for p in result.members] == ["faults.geojson"]


class TestZipSlipGuardIsAnchoredOnTheSeparator:
    """``startswith(str(root))`` also accepts a sibling of the root.

    Verified against the live function before the fix: a member named
    ``../_unzipped_evil/pwn.geojson`` was written. It landed in the
    enclosing ``mkdtemp`` rather than anywhere dangerous, but that
    containment is an accident of where the caller puts ``dest``, not
    something the guard established.
    """

    def test_a_sibling_prefixed_member_is_not_written(self, tmp_path) -> None:
        path = tmp_path / "slip.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../_unzipped_evil/pwn.geojson", _GEOJSON)
            zf.writestr("good.geojson", _GEOJSON)
        dest = tmp_path / "run" / "_unzipped"

        result = sp._extract_archive(path, dest)

        assert not (tmp_path / "run" / "_unzipped_evil" / "pwn.geojson").exists()
        assert [p.name for p in result.members] == ["good.geojson"]

    def test_a_classic_traversal_is_still_refused(self, tmp_path) -> None:
        path = tmp_path / "slip2.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../../etc/passwd", b"root:x:0:0")
            zf.writestr("good.geojson", _GEOJSON)
        dest = tmp_path / "run" / "_unzipped"

        result = sp._extract_archive(path, dest)

        assert [p.name for p in result.members] == ["good.geojson"]
        assert any(
            w["code"] == "archive_member_refused" for w in result.warnings
        )

    def test_the_refusal_reaches_the_run_not_only_the_log(
        self, tmp_path,
    ) -> None:
        """A skipped member used to be a log line on a container whose logs
        nobody reads, and the run still reported a clean ingest."""
        path = tmp_path / "slip3.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../_unzipped_evil/pwn.geojson", _GEOJSON)
            zf.writestr("good.geojson", _GEOJSON)

        result = sp._extract_archive(path, tmp_path / "run" / "_unzipped")

        assert len(result.warnings) == 1
        warning = result.warnings[0]
        assert warning["code"] == "archive_member_refused"
        assert warning["member"] == "../_unzipped_evil/pwn.geojson"
        assert "fresh copy" in warning["detail"]


class TestMembersAreStreamedNotBuffered:
    def test_the_extractor_uses_copyfileobj(self, tmp_path) -> None:
        """``src.read()`` made one member wholly resident.

        A single 1.5 GiB raster or FileGDB table inside an otherwise-legal
        archive peaked at its full size on an 8 Gi worker. The sibling
        extractor in ingest_zip_archive uses copyfileobj for this reason.
        """
        import inspect

        source = inspect.getsource(sp._extract_archive)
        assert "shutil.copyfileobj" in source
        # Scoped to code, not prose: the comment above the write still
        # names `src.read()`, which is how anyone reading it learns what
        # changed and why.
        code = [
            ln for ln in source.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        assert not any("src.read()" in ln for ln in code)

    def test_a_large_member_round_trips_byte_for_byte(self, tmp_path) -> None:
        payload = bytes(range(256)) * 8_000  # ~2 MB, non-repeating
        archive = _zip(tmp_path / "big.zip", [
            ("faults.geojson", _GEOJSON),
            ("faults.dbf", payload),
        ])

        sp._extract_archive(archive, tmp_path / "out")

        assert (tmp_path / "out" / "faults.dbf").read_bytes() == payload


class TestSidecarsStillBehave:
    """The extractor's whole reason for existing: write the sidecars, return
    only the members pyogrio should be pointed at."""

    def test_shapefile_sidecars_are_written_but_not_returned(
        self, tmp_path,
    ) -> None:
        archive = _zip(tmp_path / "shp.zip", [
            ("faults.shp", b"shp"),
            ("faults.dbf", b"dbf"),
            ("faults.shx", b"shx"),
            ("faults.prj", b"prj"),
            ("readme.txt", b"hi"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["faults.shp"]
        on_disk = sorted(p.name for p in (tmp_path / "out").rglob("*"))
        assert on_disk == [
            "faults.dbf", "faults.prj", "faults.shp", "faults.shx", "readme.txt",
        ]

    def test_appledouble_forks_are_ignored(self, tmp_path) -> None:
        archive = _zip(tmp_path / "mac.zip", [
            ("faults.geojson", _GEOJSON),
            ("__MACOSX/._faults.geojson", b"\x00"),
            ("._faults.geojson", b"\x00"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["faults.geojson"]

    def test_a_zipped_filegdb_directory_is_returned_as_one_member(
        self, tmp_path,
    ) -> None:
        """FileGDB is the standard Esri delivery and is a DIRECTORY, so its
        internal tables must not each come back as members."""
        archive = _zip(tmp_path / "gdb.zip", [
            ("survey.gdb/a00000001.gdbtable", b"t"),
            ("survey.gdb/a00000001.gdbtablx", b"x"),
            ("survey.gdb/timestamps", b"ts"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["survey.gdb"]
        assert result.members[0].is_dir()


class TestMemberSelectionBeyondTheKnownTraps:
    """The cases the three classes above do not reach.

    Added 2026-08-21 alongside the geology-workflow test port. Each one is
    a way ``_is_member`` can return the wrong SET rather than the wrong
    file: too many members, too few, or the same layer twice.
    """

    def test_filegdb_internals_are_not_also_members(self, tmp_path) -> None:
        """A .gdb whose internals include something that parses.

        The sibling test above uses .gdbtable internals, which match no
        extension and would be filtered anyway. This is the case the
        ``gdb_roots`` filter actually exists for: returning both the
        directory and a file inside it reads the same data twice, under
        two different layer names.
        """
        archive = _zip(tmp_path / "gdb2.zip", [
            ("survey.gdb/a00000001.gdbtable", b"t"),
            ("survey.gdb/nested.geojson", _GEOJSON),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["survey.gdb"]

    def test_unrecognised_members_are_left_alone_without_a_warning(
        self, tmp_path,
    ) -> None:
        """READMEs and metadata XML are not guessed at, and not complained
        about either -- every real delivery carries them, so warning on
        them would bury the warnings that matter."""
        archive = _zip(tmp_path / "mixed.zip", [
            ("README.txt", b"hello"),
            ("metadata.xml", b"<x/>"),
            ("faults.geojson", _GEOJSON),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["faults.geojson"]
        assert result.warnings == []

    def test_nested_directories_are_searched(self, tmp_path) -> None:
        """Deliveries routinely arrive as a folder per theme.

        ``rglob`` is what makes this work; a ``glob`` would silently
        return nothing for the most common consultant hand-over shape.
        """
        archive = _zip(tmp_path / "nested.zip", [
            ("GIS/structural/faults.geojson", _GEOJSON),
            ("GIS/claims/tenure.geojson", _GEOJSON),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert sorted(p.name for p in result.members) == [
            "faults.geojson", "tenure.geojson",
        ]

    def test_members_are_sorted_so_the_run_report_is_stable(
        self, tmp_path,
    ) -> None:
        """Two ingests of one archive must list layers in the same order,
        or a diff of two run reports is noise."""
        archive = _zip(tmp_path / "order.zip", [
            ("z.geojson", _GEOJSON),
            ("a.geojson", _GEOJSON),
            ("m.geojson", _GEOJSON),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == [
            "a.geojson", "m.geojson", "z.geojson",
        ]

    def test_the_entry_cap_default_is_not_absurdly_low(self) -> None:
        """Guards the guard. TestEntryCountCap monkeypatches the cap down
        to 2 to exercise the branch, which passes just as happily if the
        real default were also 2 -- and that would refuse every genuine
        multi-layer delivery."""
        assert sp._MAX_ARCHIVE_ENTRIES >= 1000


class _Feature:
    def __init__(self, layer: str | None, fid: int) -> None:
        self.feature_type = "outcrop"
        self.name = f"f{fid}"
        self.geometry_wkt = "POINT(-108.7 43.0)"
        self.properties = {"fid": fid}
        if layer is not None:
            self.properties["_layer_name"] = layer


class _ParseResult:
    def __init__(self, layers: list[str], per_feature: list[str | None]) -> None:
        self.source_crs = "EPSG:4326"
        self.layer_names = layers
        self.features = [_Feature(lyr, i) for i, lyr in enumerate(per_feature)]
        self.warnings: list[dict] = []
        self.empty_geom_skipped = 0


class TestLayerIdentitySurvivesTheZippedPath:
    """A QField ``eagle.gpkg`` inside a delivered ZIP holds collars,
    outcrops, structures, samples and traverses. All five were read and
    written correctly and then every row was stamped ``source_layer =
    'eagle'``, because the archive branch passes ``member.stem`` as the
    override and the override used to win unconditionally. Layer identity
    survived a direct upload of the same file and was lost only on the
    zipped path.
    """

    def test_the_report_names_the_real_layers_not_the_file_stem(self) -> None:
        result = _ParseResult(
            ["collars", "outcrops", "structures"],
            ["collars", "outcrops", "structures"],
        )
        assert sp._reported_layers(result, "eagle") == [
            "collars", "outcrops", "structures",
        ]

    def test_a_single_layer_member_is_still_named_after_its_file(self) -> None:
        """``layer_names`` is empty for the single-layer drivers, so a lone
        ``faults.shp`` inside a bundle keeps its own name."""
        assert sp._reported_layers(_ParseResult([], [None]), "faults") == [
            "faults",
        ]

    def test_a_direct_upload_with_no_override_reports_its_layers(self) -> None:
        result = _ParseResult(["collars", "samples"], ["collars", "samples"])
        assert sp._reported_layers(result, None) == ["collars", "samples"]

    def test_nothing_is_reported_when_there_is_nothing_to_name(self) -> None:
        assert sp._reported_layers(_ParseResult([], [None]), None) == []


class _FakeConn:
    def __init__(self) -> None:
        self.batches: list[list[tuple]] = []

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.batches.append(list(rows))

    @property
    def rows(self) -> list[tuple]:
        return [row for batch in self.batches for row in batch]


class TestWrittenRowsAgreeWithTheReport:
    """The report and the rows are read side by side on the Ingestion Runs
    page. Locking only one of the two is how they came to disagree."""

    @pytest.mark.asyncio
    async def test_per_feature_layers_beat_the_override(self) -> None:
        conn = _FakeConn()
        result = _ParseResult(
            ["collars", "structures"], ["collars", "structures"],
        )

        written = await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=result,
            source_file="delivery.zip",
            source_file_sha256="f" * 64,
            source_label="GPKG",
            layer_override="eagle",
            georef_method="native_crs",
            crs_confidence=1.0,
        )

        assert written == 2
        source_layers = [row[7] for row in conn.rows]
        assert source_layers == ["collars", "structures"]
        assert sp._reported_layers(result, "eagle") == source_layers

    @pytest.mark.asyncio
    async def test_the_override_still_names_a_lone_shapefile(self) -> None:
        conn = _FakeConn()
        result = _ParseResult([], [None, None])

        await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=result,
            source_file="delivery.zip",
            source_file_sha256="f" * 64,
            source_label="ESRI Shapefile",
            layer_override="faults",
            georef_method="native_crs",
            crs_confidence=1.0,
        )

        assert {row[7] for row in conn.rows} == {"faults"}
        assert sp._reported_layers(result, "faults") == ["faults"]

    @pytest.mark.asyncio
    async def test_the_bookkeeping_column_never_reaches_the_jsonb(self) -> None:
        """``_layer_name`` is the parser's own column. It becomes
        source_layer, so leaving it in properties duplicates it into every
        row's jsonb — and it stayed there for every row that had a named
        layer, because the pop was short-circuited by the override."""
        conn = _FakeConn()

        await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=_ParseResult(["collars"], ["collars"]),
            source_file="delivery.zip",
            source_file_sha256="f" * 64,
            source_label="GPKG",
            layer_override="eagle",
            georef_method="native_crs",
            crs_confidence=1.0,
        )

        assert "_layer_name" not in conn.rows[0][9]

    @pytest.mark.asyncio
    async def test_feature_id_zero_is_not_turned_into_null(self) -> None:
        """0 is a perfectly ordinary first fid in a shapefile."""
        conn = _FakeConn()

        await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=_ParseResult([], [None]),
            source_file="faults.shp",
            source_file_sha256="a" * 64,
            source_label="ESRI Shapefile",
            layer_override="faults",
            georef_method="native_crs",
            crs_confidence=1.0,
        )

        assert conn.rows[0][8] == "0"


class TestSourceFileHashIsRecorded:
    """silver.spatial_features.source_file_sha256 was NULL on every row.

    The column exists, carries an index (idx_spatial_features_source_sha)
    and a COMMENT reading "joins to bronze.ingest_manifest for full
    provenance" -- and the live INSERT never bound it, so the documented
    join found nothing and the index covered a column of nulls. This is
    the cheap half of DM-11: for spatial data every other lineage column
    (source_file, source_layer, source_feature_id, source_crs) was
    already populated and only the hash was missing.
    """

    @pytest.mark.asyncio
    async def test_the_hash_reaches_every_row(self) -> None:
        conn = _FakeConn()
        result = _ParseResult(["faults"], ["faults", "faults", "faults"])

        await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=result,
            source_file="delivery.zip",
            source_file_sha256="b" * 64,
            source_label="GPKG",
            layer_override="delivery",
            georef_method="declared",
            crs_confidence=1.0,
        )

        assert {row[14] for row in conn.rows} == {"b" * 64}
        assert len(conn.rows) == 3

    @pytest.mark.asyncio
    async def test_appending_the_hash_did_not_shift_the_other_columns(
        self,
    ) -> None:
        """$15 was appended rather than slotted in numerically.

        Renumbering fourteen placeholders to insert one in the middle is a
        silent column-shuffle waiting to happen -- the INSERT stays valid
        and the data goes to the wrong columns. This pins the positions
        the rest of this file already relies on.
        """
        conn = _FakeConn()
        result = _ParseResult(["faults"], ["faults"])

        await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=result,
            source_file="delivery.zip",
            source_file_sha256="c" * 64,
            source_label="GPKG",
            layer_override=None,
            georef_method="declared",
            crs_confidence=0.9,
        )

        row = conn.rows[0]
        assert row[0] == "a0000000-0000-0000-0000-00000000feed"  # workspace
        assert row[1] == "b1000000-0000-0000-0000-0000000000a0"  # project
        assert row[5] == "delivery.zip"                          # source_file
        assert row[7] == "faults"                                # source_layer
        assert row[13].startswith("POINT")                       # geometry_wkt
        assert row[14] == "c" * 64                               # the new one

    @pytest.mark.asyncio
    async def test_a_caller_with_no_hash_writes_null_not_a_wrong_one(
        self,
    ) -> None:
        """No caller does this today. If one appears, NULL is the honest
        answer -- a placeholder hash would silently poison the dedup
        index the column exists to serve."""
        conn = _FakeConn()

        await sp._write_features(
            conn,
            workspace_id="a0000000-0000-0000-0000-00000000feed",
            project_id="b1000000-0000-0000-0000-0000000000a0",
            parse_result=_ParseResult(["faults"], ["faults"]),
            source_file="delivery.zip",
            source_file_sha256=None,
            source_label="GPKG",
            layer_override=None,
            georef_method="declared",
            crs_confidence=0.9,
        )

        assert conn.rows[0][14] is None


class TestShaHelper:
    def test_it_matches_hashlib_over_the_whole_file(self, tmp_path) -> None:
        import hashlib

        payload = b"".join(bytes([i % 256]) for i in range(3 * 1024 * 1024 + 7))
        target = tmp_path / "delivery.zip"
        target.write_bytes(payload)

        assert sp._sha256_file(target) == hashlib.sha256(payload).hexdigest()

    def test_it_streams_rather_than_reading_the_file_whole(self) -> None:
        """A delivery is capped at 2 GiB and this runs on an 8 Gi worker.

        Asserted on the source rather than by measuring RSS, which is not
        reliable in a test: the point is that nobody reintroduces
        ``read_bytes()`` here, which is the exact mistake
        ``_extract_archive`` already had to unwind.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sp._sha256_file).lstrip())
        # The docstring MENTIONS read_bytes as the thing not to do, so a
        # substring check over the raw source fails on its own explanation.
        # Look at the calls the function actually makes.
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "read_bytes" not in calls
        assert "iter" in names, "the chunked read is what makes this streaming"

    def test_an_empty_file_still_hashes(self, tmp_path) -> None:
        import hashlib

        target = tmp_path / "empty.zip"
        target.write_bytes(b"")
        assert sp._sha256_file(target) == hashlib.sha256(b"").hexdigest()


class TestTheCallerConsumesTheStructuredResult:
    def test_the_archive_branch_forwards_extraction_warnings(self) -> None:
        source = Path(sp.__file__).read_text(encoding="utf-8")
        assert "warnings.extend(extraction.warnings)" in source, (
            "a member the extractor declined to write must reach the run's "
            "warnings — mark_completed_by_run downgrades the terminal status "
            "to 'partial' on the strength of that list, which is the "
            "difference between a delivery that ingested cleanly and one "
            "that ingested in part"
        )
class TestWhatCameWithEachShapefile:
    """``members`` answers "what should be opened". It cannot also answer
    "what arrived", and after SHAPE_RESTORE_SHX the second question is the
    one that matters.

    A lone ``.shp`` inside a zip is now READABLE -- GDAL rebuilds the index
    from the ``.shp`` itself -- so the extractor can no longer refuse it,
    and nothing downstream can work out what was in the delivery either: by
    the time the parser has run, GDAL may have WRITTEN the ``.shx`` that was
    never sent. Only the extractor sees the archive as it arrived.

    Four tests in this file pin ``members == ['faults.shp']``. That stays
    true; the inventory rides alongside it on the dataclass that exists for
    exactly this purpose.
    """

    def test_a_complete_bundle_lists_its_sidecars(self, tmp_path) -> None:
        archive = _zip(tmp_path / "shp.zip", [
            ("faults.shp", b"shp"),
            ("faults.dbf", b"dbf"),
            ("faults.shx", b"shx"),
            ("faults.prj", b"prj"),
            ("faults.cpg", b"UTF-8"),
            ("readme.txt", b"hi"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["faults.shp"]
        assert list(result.companions.values()) == [
            [".cpg", ".dbf", ".prj", ".shx"],
        ]

    def test_a_lone_shapefile_is_returned_with_an_empty_inventory(
        self, tmp_path,
    ) -> None:
        """Four of the eight archives in the RedStar delivery are exactly
        this: a ``.shp`` zipped on its own. Refusing them was the old
        behaviour and it refused readable data; the inventory is what lets
        the CRS gate say WHICH file is missing instead of failing with
        GDAL's message about a config option."""
        archive = _zip(tmp_path / "lone.zip", [("faults.shp", b"shp")])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert [p.name for p in result.members] == ["faults.shp"]
        assert list(result.companions.values()) == [[]]

    def test_a_mis_cased_prj_is_not_reported_as_absent(self, tmp_path) -> None:
        """GDAL on Linux is case-sensitive; this inventory is not, on
        purpose. The real delivery holds ``drobeck_shumagin_veins.shp``
        beside ``Drobeck_Shumagin_Veins.prj``. Resolving that case is
        somebody else's job -- what this must not do is tell a geologist to
        send a file that is sitting in the zip they already sent, because a
        missing CRS is now a refusal."""
        archive = _zip(tmp_path / "case.zip", [
            ("drobeck_shumagin_veins.shp", b"shp"),
            ("Drobeck_Shumagin_Veins.PRJ", b"prj"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert list(result.companions.values()) == [[".prj"]]

    def test_a_same_stem_file_that_is_not_a_sidecar_is_not_counted(
        self, tmp_path,
    ) -> None:
        archive = _zip(tmp_path / "extra.zip", [
            ("faults.shp", b"shp"),
            ("faults.prj", b"prj"),
            ("faults.xml", b"<x/>"),
            ("faults.txt", b"notes"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert list(result.companions.values()) == [[".prj"]]

    def test_sidecars_of_a_different_layer_are_not_borrowed(
        self, tmp_path,
    ) -> None:
        """Two shapefiles in one folder, one complete and one not. Reading
        the folder rather than the stem would report the incomplete layer
        as having a .prj and write it at longitude 400,798."""
        archive = _zip(tmp_path / "two.zip", [
            ("claims.shp", b"shp"), ("claims.prj", b"prj"),
            ("faults.shp", b"shp"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        by_name = {
            Path(k).name: v for k, v in result.companions.items()
        }
        assert by_name == {"claims.shp": [".prj"], "faults.shp": []}

    def test_same_stem_layers_in_different_folders_stay_separate(
        self, tmp_path,
    ) -> None:
        """Deliveries arrive as a folder per theme, and the same stem in two
        of them is normal. Keying the inventory on the name rather than the
        path would merge them and lend one layer the other's .prj."""
        archive = _zip(tmp_path / "themes.zip", [
            ("GIS/structural/faults.shp", b"shp"),
            ("GIS/structural/faults.prj", b"prj"),
            ("GIS/claims/faults.shp", b"shp"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert sorted(result.companions.values()) == [[], [".prj"]]

    def test_non_shapefile_members_get_no_entry(self, tmp_path) -> None:
        """A GeoJSON has no sidecars to be missing. An empty entry would
        read as "delivered incomplete"."""
        archive = _zip(tmp_path / "gj.zip", [("faults.geojson", _GEOJSON)])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert result.companions == {}

    def test_the_inventory_keys_are_the_member_paths(self, tmp_path) -> None:
        """The caller looks these up by the member it is about to parse."""
        archive = _zip(tmp_path / "keys.zip", [
            ("faults.shp", b"shp"), ("faults.prj", b"prj"),
        ])

        result = sp._extract_archive(archive, tmp_path / "out")

        assert set(result.companions) == {str(result.members[0])}


def _workflow_ast() -> ast.AsyncFunctionDef:
    """The task body, parsed. Source-scanned rather than executed: running
    it needs Hatchet, object storage and Postgres, and the properties below
    are structural."""
    tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_ingest_spatial":
            return node
    raise AssertionError("run_ingest_spatial not found — renamed?")


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


class TestTheLoneShapefileGuardIsInverted:
    """A bare ``.shp`` used to be refused before the download.

    That was right when it was written and is wrong now. MEASURED with
    SHAPE_RESTORE_SHX on: a ``.shp`` with no sidecars at all opens and
    yields every feature, because GDAL regenerates the index from the
    ``.shp`` itself. The guard refused data the pipeline can read -- and it
    never caught the case that actually happens, because both upload
    wizards zip a bare ``.shp`` before sending it and ingest_zip_archive
    re-zips one itself, so the real shape is a lone ``.shp`` INSIDE an
    archive, which that branch never saw.

    What no config option recovers is the ``.prj``, so the refusal moved
    rather than disappeared.
    """

    def test_no_branch_refuses_a_file_for_being_a_shapefile(self) -> None:
        tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "suffix"
            and any(
                isinstance(c, ast.Constant) and c.value == ".shp"
                for c in node.comparators
            )
        ]
        assert offenders == [], (
            f"line(s) {offenders} branch on the upload being a lone .shp. "
            "GDAL reads one now; refusing it rejects data the pipeline can "
            "handle. The refusal belongs on the missing CRS, which is the "
            "part nothing can reconstruct."
        )

    def test_shp_is_still_a_supported_extension(self) -> None:
        """Guards the guard: the check above also passes if .shp stops
        being routed here at all, which would be a worse bug."""
        assert ".shp" in sp.SUPPORTED_EXTENSIONS
        assert ".shp" in sp.VECTOR_EXTENSIONS


class TestTheRefusalRunsWhereARollbackIsStillPossible:
    def test_the_crs_gate_is_inside_the_write_transaction(self) -> None:
        """A refused delivery must not cost the geologist the PREVIOUS,
        good ingest of the same file. The write path deletes the old rows
        and re-inserts, so a gate outside that transaction would leave the
        delete committed and nothing to replace it."""
        transactions = [
            node for node in ast.walk(_workflow_ast())
            if isinstance(node, ast.AsyncWith)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "transaction"
                for item in node.items
            )
        ]
        assert transactions, "the write transaction is gone — moved?"
        assert any(
            "_crs_refusal" in _called_names(node) for node in transactions
        ), (
            "the CRS gate must run inside the delete-then-reinsert "
            "transaction, so refusing a file rolls the delete back"
        )

    def test_the_gate_raises_rather_than_warning(self) -> None:
        """'partial' would not do. Laravel's DATA_LANDED_STATUSES is
        ['completed','partial'], so a partial with zero rows still bumps
        data_version and fires the MV refresh."""
        node = _workflow_ast()
        raises_on_refusal = [
            n for n in ast.walk(node)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == "refusal"
            and any(isinstance(b, ast.Raise) for b in n.body)
        ]
        assert raises_on_refusal, "a refusal must raise, not attach a warning"


class TestTheFailurePathTellsSomebody:
    def test_the_handler_broadcasts_as_well_as_recording(self) -> None:
        """No ingest workflow broadcast on failure before this one: the row
        went to 'failed' in Postgres and the page showing it was never
        told, so a refused upload sat on screen as "running" until somebody
        reloaded. A refusal nobody sees is not much better than the silent
        corruption it replaced."""
        handlers = [
            h for h in ast.walk(_workflow_ast())
            if isinstance(h, ast.ExceptHandler)
            and "mark_failed_by_run" in _called_names(h)
        ]
        assert handlers, "the failure handler no longer records the failure"
        for handler in handlers:
            assert "broadcast_terminal" in _called_names(handler)

    def test_the_broadcast_status_is_failed(self) -> None:
        """'failed' is accepted by the Laravel validator and is correctly
        OUTSIDE DATA_LANDED_STATUSES — it notifies without bumping
        data_version or refreshing the materialised views, which is what a
        run that deliberately wrote nothing needs."""
        broadcasts = [
            n for n in ast.walk(_workflow_ast())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "broadcast_terminal"
        ]
        statuses = [
            kw.value.value
            for call in broadcasts
            for kw in call.keywords
            if kw.arg == "status" and isinstance(kw.value, ast.Constant)
        ]
        assert "failed" in statuses


class TestTheParseLoopKeepsTheRunAlive:
    def test_the_member_loop_beats(self) -> None:
        """There was no heartbeat at all between 'parse' and 'persist', and
        the stale sweep times a run out after fifteen silent minutes.
        SHAPE_RESTORE_SHX rebuilding a large index widens that gap, and a
        run relabelled 'timed_out' hands the geologist a failure for a file
        that is still ingesting."""
        loops = [
            n for n in ast.walk(_workflow_ast())
            if isinstance(n, ast.For)
            and "mark_stage_progress" in _called_names(n)
        ]
        assert loops, (
            "no per-member heartbeat: the parse stage can run for minutes "
            "with nothing feeding last_heartbeat_at"
        )


class TestTheOverrideReachesTheParser:
    def test_every_parse_call_forwards_source_epsg(self) -> None:
        """Four call sites — QGIS project layers, a project inside a zip, an
        archive member, a direct upload. An override wired into three of
        them is a field that works depending on how the file was delivered,
        which is worse than one that does not work at all."""
        calls = [
            n for n in ast.walk(_workflow_ast())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "to_thread"
        ]
        parse_calls = [
            n for n in calls
            if any(
                isinstance(a, ast.Name) and a.id == "parse_spatial_file"
                for a in n.args
            )
        ]
        assert len(parse_calls) == 4, (
            f"expected 4 parse_spatial_file call sites, found "
            f"{len(parse_calls)} — a new one needs the override too"
        )
        for call in parse_calls:
            assert "source_epsg" in {kw.arg for kw in call.keywords}, (
                f"the call at line {call.lineno} drops the uploader's CRS"
            )
