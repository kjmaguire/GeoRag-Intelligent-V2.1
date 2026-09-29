"""Every ZIP member finds a home, or is named -- nothing is silently dropped.

Members that used to fall to ``unknown`` inside an archive:

  * a nested ``.zip`` (a delivery of zips) -- now unpacked in place, under one
    entry/size budget shared with the outer archive and a depth limit;
  * an Esri File Geodatabase (a ``.gdb`` FOLDER) -- now re-zipped with its own
    name on top and handed to ingest_spatial, its inner files not members;
  * ``.txt`` -- now routed to ingest_tabular (typed if delimited drill data,
    indexed as text otherwise);
  * ``.mdb`` / ``.accdb`` -- now routed to ingest_tabular like a standalone
    ``.dbf``.

What still cannot be ingested is reported by ``archive_member_unhandled``
together with the fact that the original archive stays in bronze.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import ingest_zip_archive as module

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000007"


def _zip_bytes(members: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _extract(tmp_path: Path, members: dict[str, bytes | str]) -> tuple[Path, module._ExtractBudget]:
    outer = tmp_path / "in.zip"
    outer.write_bytes(_zip_bytes(members))
    root = tmp_path / "x"
    budget = module._ExtractBudget()
    module._extract_zip_into(outer, root, budget)
    return root, budget


# ---------------------------------------------------------------------------
# Nested archives
# ---------------------------------------------------------------------------


def test_a_nested_zip_is_unpacked_in_place_and_its_members_become_members(tmp_path: Path) -> None:
    inner = _zip_bytes({"collars.csv": "hole_id,easting,northing\n", "deep/log.txt": "notes"})
    root, budget = _extract(tmp_path, {"top.pdf": "%PDF", "delivery/part1.zip": inner})

    warnings = module._expand_nested_archives(root, budget)

    assert warnings == []
    names = sorted(p.name for p in module._collect_members(root))
    assert names == ["collars.csv", "log.txt", "top.pdf"]
    assert not list(root.rglob("*.zip")), "the expanded zip must not linger as an 'unknown' member"


def test_zips_nested_three_deep_are_all_opened(tmp_path: Path) -> None:
    level3 = _zip_bytes({"leaf.csv": "a,b\n"})
    level2 = _zip_bytes({"l3.zip": level3})
    level1 = _zip_bytes({"l2.zip": level2})
    root, budget = _extract(tmp_path, {"l1.zip": level1})

    assert module._expand_nested_archives(root, budget) == []
    assert [p.name for p in module._collect_members(root)] == ["leaf.csv"]


def test_a_zip_nested_deeper_than_the_limit_is_left_and_named(tmp_path: Path) -> None:
    payload: bytes = _zip_bytes({"bottom.csv": "a,b\n"})
    for i in range(module._MAX_NESTED_DEPTH + 1):
        payload = _zip_bytes({f"z{i}.zip": payload})
    root, budget = _extract(tmp_path, {"outer.zip": payload})

    warnings = module._expand_nested_archives(root, budget)

    assert [w["code"] for w in warnings] == ["archive_nested_zip_not_expanded"]
    assert "deep" in warnings[0]["detail"]
    assert list(root.rglob("*.zip")), "the unexpanded zip stays visible as a member"


def test_a_corrupt_nested_zip_does_not_abort_the_archive(tmp_path: Path) -> None:
    root, budget = _extract(tmp_path, {
        "good.csv": "a,b\n", "broken.zip": b"this is not a zip file",
        "ok.zip": _zip_bytes({"inner.csv": "a,b\n"}),
    })

    warnings = module._expand_nested_archives(root, budget)

    assert [(w["code"], w["file"]) for w in warnings] == [("archive_nested_zip_not_expanded", "broken.zip")]
    assert sorted(p.name for p in module._collect_members(root)) == ["broken.zip", "good.csv", "inner.csv"]


def test_a_zip_slip_inside_a_nested_zip_is_refused_not_written(tmp_path: Path) -> None:
    evil = _zip_bytes({"../../escaped.txt": "x"})
    root, budget = _extract(tmp_path, {"evil.zip": evil, "fine.csv": "a,b\n"})

    warnings = module._expand_nested_archives(root, budget)

    assert [w["code"] for w in warnings] == ["archive_nested_zip_not_expanded"]
    assert "zip-slip" in warnings[0]["detail"]
    assert not (tmp_path / "escaped.txt").exists() and not (root.parent / "escaped.txt").exists()


def test_the_entry_budget_is_shared_with_the_outer_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_MAX_ENTRIES", 4)
    inner = _zip_bytes({f"f{i}.csv": "a\n" for i in range(3)})
    root, budget = _extract(tmp_path, {"a.csv": "a\n", "inner.zip": inner})  # 2 outer entries

    warnings = module._expand_nested_archives(root, budget)  # 2 + 3 > 4

    assert [w["code"] for w in warnings] == ["archive_nested_zip_not_expanded"]
    assert "zip-bomb guard" in warnings[0]["detail"]


# ---------------------------------------------------------------------------
# Members: geodatabase folders, AppleDouble junk
# ---------------------------------------------------------------------------


def test_a_gdb_folder_is_one_member_and_its_inner_files_are_not(tmp_path: Path) -> None:
    root, _ = _extract(tmp_path, {
        "data/site.gdb/a00000001.gdbtable": b"\x00", "data/site.gdb/a00000001.gdbtablx": b"\x00",
        "data/site.gdb/gdb": b"\x00", "readme.txt": "hi",
    })

    members = module._collect_members(root)

    assert sorted(p.name for p in members) == ["readme.txt", "site.gdb"]
    assert next(p for p in members if p.name == "site.gdb").is_dir()


def test_appledouble_junk_is_not_a_member(tmp_path: Path) -> None:
    root, _ = _extract(tmp_path, {
        "collars.csv": "a\n", "._collars.csv": b"\x00\x05", "__MACOSX/x/._y.pdf": b"\x00",
    })

    assert [p.name for p in module._collect_members(root)] == ["collars.csv"]


# ---------------------------------------------------------------------------
# _ingest_one routing
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket: Any, key: str, data: bytes) -> None:
        self.objects[key] = data


def _input() -> Any:
    return module.IngestZipArchiveInput(
        minio_key="zips/a.zip", workspace_id=_WS, project_id=_PJ, run_id=_RUN,
    )


async def _route(
    monkeypatch: pytest.MonkeyPatch, path: Path,
) -> tuple[dict[str, int], list[tuple[str, Any]], _Store]:
    sent: list[tuple[str, Any]] = []

    def recorder(name: str) -> Any:
        async def aio_run_no_wait(payload: Any) -> Any:
            sent.append((name, payload))
            return SimpleNamespace(workflow_run_id=f"wf-{len(sent)}")

        return aio_run_no_wait

    monkeypatch.setattr(module.ingest_tabular, "aio_run_no_wait", recorder("tabular"))
    monkeypatch.setattr(module.ingest_spatial, "aio_run_no_wait", recorder("spatial"))

    async def no_sleep(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    counts = dict.fromkeys(module._COUNT_KEYS, 0)
    store = _Store()
    await module._ingest_one(
        file_path=path, ext=path.suffix.lower().lstrip("."), conn=SimpleNamespace(),  # type: ignore[arg-type]
        store=store, input=_input(), counts=counts,  # type: ignore[arg-type]
    )
    return counts, sent, store


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["notes.txt", "drill.TXT"])
async def test_txt_goes_to_ingest_tabular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    f = tmp_path / name
    f.write_text("hole_id\teasting\tnorthing\nH1\t1\t2\n", encoding="utf-8")

    counts, sent, store = await _route(monkeypatch, f)

    assert [n for n, _ in sent] == ["tabular"]
    assert counts["csv"] == 1 and counts["unknown"] == 0
    assert list(store.objects)[0].startswith(f"tabular/{_PJ}/")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["survey.mdb", "survey.ACCDB"])
async def test_access_databases_go_to_ingest_tabular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    f = tmp_path / name
    f.write_bytes(b"\x00\x01\x00\x00Standard Jet DB")

    counts, sent, _ = await _route(monkeypatch, f)

    assert [n for n, _ in sent] == ["tabular"]
    assert counts["tabular"] == 1 and counts["unknown"] == 0


@pytest.mark.asyncio
async def test_a_gdb_folder_is_rezipped_with_its_name_on_top_and_sent_to_ingest_spatial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gdb = tmp_path / "Site.gdb"
    gdb.mkdir()
    (gdb / "a00000001.gdbtable").write_bytes(b"table")
    (gdb / "gdb").write_bytes(b"x")

    counts, sent, store = await _route(monkeypatch, gdb)

    assert [n for n, _ in sent] == ["spatial"]
    assert counts["spatial"] == 1 and counts["unknown"] == 0 and counts["errors"] == 0
    (key, data), = store.objects.items()
    assert key.startswith(f"spatial/{_PJ}/") and key.endswith("_Site.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["Site.gdb/a00000001.gdbtable", "Site.gdb/gdb"]
    assert not list(tmp_path.glob("__bundle_*")), "the temporary bundle is cleaned up"


# ---------------------------------------------------------------------------
# What still has no ingester is named, and the archive is said to be kept
# ---------------------------------------------------------------------------


def test_unhandled_members_are_named_and_the_archive_is_said_to_be_kept() -> None:
    counts = dict.fromkeys(module._COUNT_KEYS, 0)
    (warning,) = module._archive_warnings(
        total=2, counts=counts, errors=[], unhandled=["scan.dwg"], archive_key="zips/a.zip",
    )

    assert warning["code"] == "archive_member_unhandled"
    assert "scan.dwg" in warning["detail"] and "zips/a.zip" in warning["detail"]
    assert "not lost" in warning["detail"]


def test_nested_zip_warning_reads_as_one_line_per_archive() -> None:
    (summary,) = module._member_warning_summaries([
        {"code": "archive_nested_zip_not_expanded", "detail": "d", "file": "a.zip"},
        {"code": "archive_nested_zip_not_expanded", "detail": "d", "file": "b.zip"},
    ])
    assert "2 zip file(s)" in summary["detail"] and "a.zip" in summary["detail"]
