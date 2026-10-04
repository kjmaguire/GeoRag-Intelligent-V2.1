"""Tabular ZIP members carry their directory tag, and long names keep it.

Audit 2026-10-04 (F4): ``Au/assays.csv`` and ``Cu/assays.csv`` both reached
ingest_tabular as ``<stamp>_assays.csv``. Its replace key is the LOGICAL file
name (stamp stripped), so the two shared one ``source_file`` and each upload
deleted the other's rows for the same holes. The spatial branch already tagged
members with a short directory hash; the tabular branch did not. And
``_safe_filename`` cut at 120 characters from the END, taking the extension and
the tag with it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import ingest_tabular as tabular_mod
from app.hatchet_workflows import ingest_zip_archive as module

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"
_RUN = "c2000000-0000-0000-0000-000000000007"


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket: Any, key: str, data: bytes) -> None:
        self.objects[key] = data

    def put_file(self, bucket: Any, key: str, file_path: str) -> None:
        self.objects[key] = Path(file_path).read_bytes()


async def _route_members(
    monkeypatch: pytest.MonkeyPatch, root: Path, rel_paths: list[str],
) -> list[str]:
    """Run each member through _ingest_one; return the tabular keys it uploaded."""

    async def aio_run_no_wait(payload: Any) -> Any:
        return SimpleNamespace(workflow_run_id="wf-1")

    monkeypatch.setattr(module.ingest_tabular, "aio_run_no_wait", aio_run_no_wait)

    async def no_sleep(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    store = _Store()
    keys: list[str] = []
    for rel in rel_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("hole_id,from,to,Au_ppm\nH1,0,1,1.2\n", encoding="utf-8")
        before = set(store.objects)
        await module._ingest_one(
            file_path=path, ext=path.suffix.lower().lstrip("."),
            conn=SimpleNamespace(),  # type: ignore[arg-type]
            store=store, input=module.IngestZipArchiveInput(  # type: ignore[arg-type]
                minio_key="zips/a.zip", workspace_id=_WS, project_id=_PJ, run_id=_RUN,
            ),
            counts=dict.fromkeys(module._COUNT_KEYS, 0),  # type: ignore[arg-type]
            archive_root=root,
        )
        (new_key,) = set(store.objects) - before
        keys.append(new_key)
    return keys


def _logical(key: str) -> str:
    return tabular_mod._logical_source_name(key.rsplit("/", 1)[-1])


@pytest.mark.asyncio
async def test_same_named_members_in_different_directories_get_distinct_logical_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    au, cu = await _route_members(monkeypatch, tmp_path, ["Au/assays.csv", "Cu/assays.csv"])

    # The replace key ingest_tabular uses must differ, or Cu replaces Au.
    assert _logical(au) != _logical(cu)
    assert _logical(au).startswith("assays__") and _logical(au).endswith(".csv")
    assert _logical(cu).startswith("assays__") and _logical(cu).endswith(".csv")


@pytest.mark.asyncio
async def test_a_member_at_the_archive_root_keeps_its_plain_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (root_key,) = await _route_members(monkeypatch, tmp_path, ["assays.csv"])
    assert _logical(root_key) == "assays.csv"


@pytest.mark.asyncio
async def test_the_tag_is_deterministic_so_a_reupload_still_replaces_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (first,) = await _route_members(monkeypatch, tmp_path / "one", ["Au/assays.csv"])
    (second,) = await _route_members(monkeypatch, tmp_path / "two", ["Au/assays.csv"])
    assert _logical(first) == _logical(second)


@pytest.mark.asyncio
async def test_the_tag_goes_before_the_extension_so_classification_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (key,) = await _route_members(monkeypatch, tmp_path, ["Au/assays.csv"])
    assert key.startswith(f"tabular/{_PJ}/") and key.endswith(".csv")


class TestSafeFilename:
    def test_short_names_are_only_sanitised(self) -> None:
        assert module._safe_filename("my file (1).csv") == "my_file_1_.csv"

    def test_a_long_name_keeps_its_extension_and_directory_tag(self) -> None:
        name = ("x" * 200) + "__a1b2c3.csv"
        out = module._safe_filename(name)
        assert len(out) <= 120
        assert out.endswith("__a1b2c3.csv")

    def test_two_long_names_differing_only_in_their_tag_stay_distinct(self) -> None:
        base = "assay_certificate_" * 12
        a = module._safe_filename(f"{base}__aaaaaa.csv")
        b = module._safe_filename(f"{base}__bbbbbb.csv")
        assert a != b and len(a) <= 120 and len(b) <= 120

    def test_a_long_name_without_a_tag_keeps_its_extension(self) -> None:
        out = module._safe_filename(("y" * 300) + ".xlsx")
        assert len(out) == 120 and out.endswith(".xlsx")

    def test_a_long_name_with_no_extension_is_just_truncated(self) -> None:
        assert len(module._safe_filename("z" * 300)) == 120

    def test_a_pathological_extension_is_not_preserved_at_the_cost_of_the_stem(self) -> None:
        out = module._safe_filename("a." + "e" * 300)
        assert len(out) == 120
