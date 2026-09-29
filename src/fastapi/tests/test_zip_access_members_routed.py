"""An Access database inside a ZIP must reach ingest_tabular, not `unknown`.

Found tracing the RedStar delivery (Geosoft IP survey ``CEN_L3750_IP.mdb``, a
JET3 file holding 19 tables). ``ingest_tabular`` reads ``.mdb`` / ``.accdb``
through mdbtools and the upload controller's ``tables`` category accepts
them, so a loose ``.mdb`` ingests. ``ingest_zip_archive._ingest_one`` did not
list the extension, so the same file inside an archive fell to the final
``else`` and was counted ``unknown`` — never opened, never given a run, and
surfaced only as an "unhandled member" warning on an archive row that still
closed as completed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.hatchet_workflows import ingest_zip_archive as module

_WS = "a0000000-0000-0000-0000-00000000feed"
_PJ = "b1000000-0000-0000-0000-0000000000a0"


def _input() -> module.IngestZipArchiveInput:
    return module.IngestZipArchiveInput(
        minio_key="archive/x.zip",
        workspace_id=_WS,
        project_id=_PJ,
        run_id="c2000000-0000-0000-0000-0000000000b0",
        source_epsg=26904,
    )


@pytest.mark.parametrize("name", ["CEN_L3750_IP.mdb", "SURVEY.ACCDB"])
async def test_access_member_is_dispatched_to_ingest_tabular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    member = tmp_path / name
    member.write_bytes(b"\x00\x01JET3")

    dispatch = AsyncMock()
    monkeypatch.setattr(module.ingest_tabular, "aio_run_no_wait", dispatch)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    store = MagicMock()
    counts = dict.fromkeys(module._COUNT_KEYS, 0)

    await module._ingest_one(
        file_path=member,
        ext=member.suffix.lower().lstrip("."),
        conn=MagicMock(),
        store=store,
        input=_input(),
        counts=counts,
    )

    assert counts["tabular"] == 1
    assert counts["unknown"] == 0 and counts["sidecar"] == 0
    store.put_bytes.assert_called_once()
    key = store.put_bytes.call_args.args[1]
    assert key.startswith(f"tables/{_PJ}/")
    assert key.lower().endswith((".mdb", ".accdb"))
    dispatch.assert_awaited_once()
    sent = dispatch.await_args.args[0]
    assert sent.minio_key == key
    assert sent.source_epsg == 26904


async def test_unrelated_extension_is_still_unknown(
    tmp_path: Path,
) -> None:
    member = tmp_path / "notes.docx"
    member.write_bytes(b"x")
    counts = dict.fromkeys(module._COUNT_KEYS, 0)

    await module._ingest_one(
        file_path=member, ext="docx", conn=MagicMock(), store=MagicMock(),
        input=_input(), counts=counts,
    )

    assert counts["unknown"] == 1 and counts["tabular"] == 0
