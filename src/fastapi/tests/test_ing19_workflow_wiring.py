"""ING-19: the three idle parsers are reachable from the workflows.

Before 2026-09-29 an ``.xyz`` upload answered 422, and inside a ZIP an
``.xyz`` or a DCIP2D export fell to ``unknown``; a geochronology table went
to the sample classifier or the text fallback. These tests pin each route.
They import the Hatchet workflow modules, so (like every other workflow
test) they need the dummy HATCHET_CLIENT_TOKEN ci.yml sets.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.hatchet_workflows import ingest_zip_archive as zip_module

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"


def _archive_input() -> zip_module.IngestZipArchiveInput:
    return zip_module.IngestZipArchiveInput(
        minio_key=f"archive/{_PJ}/20260929_120000_Sitka_2005.zip",
        workspace_id=_WS,
        project_id=_PJ,
        run_id="c2000000-0000-0000-0000-0000000000b0",
        source_epsg=26909,
    )


def test_ingest_geophysics_is_registered_in_the_ingestion_pool() -> None:
    from app.hatchet_workflows.worker import POOLS

    names = [wf.name for wf in POOLS["ingestion"]]
    assert "ingest_geophysics" in names


def test_ingest_geophysics_has_a_trigger_route() -> None:
    from app.routers.shadow_trigger import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/internal/v1/shadow/ingest_geophysics/trigger" in paths


async def test_an_xyz_member_is_dispatched_to_ingest_geophysics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "extracted"
    member = root / "GroundMag" / "sitka_mag.xyz"
    member.parent.mkdir(parents=True)
    member.write_text("/ X Y MAG\nLine 1\n495000 6220000 55000\n", encoding="utf-8")

    dispatch = AsyncMock()
    monkeypatch.setattr(zip_module.ingest_geophysics, "aio_run_no_wait", dispatch)
    monkeypatch.setattr(zip_module.asyncio, "sleep", AsyncMock())
    store = MagicMock()
    counts = dict.fromkeys(zip_module._COUNT_KEYS, 0)

    await zip_module._ingest_one(
        file_path=member, ext="xyz", conn=MagicMock(), store=store,
        input=_archive_input(), counts=counts, archive_root=root,
    )

    assert counts["geophysics"] == 1 and counts["unknown"] == 0
    key = store.put_file.call_args.args[1]
    assert key.startswith(f"xyz/{_PJ}/") and key.endswith("sitka_mag.xyz")
    sent = dispatch.await_args.args[0]
    assert sent.minio_key == key
    assert sent.source_epsg == 26909
    # "<archive>/<path in archive>": a re-upload of the same archive replaces
    # the same survey; another archive's mag.xyz does not overwrite it.
    assert sent.source_name == "Sitka_2005.zip/GroundMag/sitka_mag.xyz"


async def test_a_dcip_export_directory_is_bundled_and_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.ingest.dcip_bundle import find_dcip_exports

    root = tmp_path / "extracted"
    export = root / "IP" / "L3750N" / "export"
    export.mkdir(parents=True)
    (export / "CEN_L3750_Vp_XYZ.rdtmd").write_text(
        "Vp - Line 3750 N\nPole-Dipole\n 4596.62 4596.62 4647.1 4697.3 0.17\n",
        encoding="ascii",
    )
    (export / "ipinv2d.chg").write_text("1 1\n5.0\n", encoding="ascii")
    exports, rest = find_dcip_exports(sorted(p for p in root.rglob("*") if p.is_file()))
    assert rest == [] and len(exports) == 1

    dispatch = AsyncMock()
    monkeypatch.setattr(zip_module.ingest_geophysics, "aio_run_no_wait", dispatch)
    uploaded: dict[str, list[str]] = {}

    def _put_file(_bucket, key, path):
        import zipfile

        with zipfile.ZipFile(path) as zf:
            uploaded[key] = zf.namelist()

    store = MagicMock()
    store.put_file.side_effect = _put_file

    await zip_module._dispatch_dcip_export(
        exports[0], root=root, store=store, input=_archive_input(), children=None,
    )

    (key, names), = uploaded.items()
    assert key.startswith(f"xyz/{_PJ}/") and key.endswith("_dcip2d.zip")
    assert sorted(names) == [
        "IP/L3750N/export/CEN_L3750_Vp_XYZ.rdtmd", "IP/L3750N/export/ipinv2d.chg",
    ]
    sent = dispatch.await_args.args[0]
    assert sent.minio_key == key and sent.source_name == "Sitka_2005.zip"


def test_ingest_tabular_routes_geochronology_by_its_headers() -> None:
    from app.hatchet_workflows import ingest_tabular as tabular

    assert tabular._routes_to_geochronology(["Sample", "System", "Age (Ma)"], "unknown")
    assert tabular._routes_to_geochronology(
        ["HoleID", "From", "To", "SampleID", "System", "Age_Ma"], "sample",
    )
    assert not tabular._routes_to_geochronology(["HoleID", "From", "To", "Au_ppm"], "sample")
    assert tabular._routes_to_geochronology(["x"], "unknown", hinted=True)
    assert tabular.GEOCHRONOLOGY_TYPE not in tabular.WRITE_ORDER
