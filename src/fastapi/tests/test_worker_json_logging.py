"""L1564 — the Hatchet worker must emit the same JSON log shape as FastAPI.

`app/logging_config.py`'s module docstring claims "both services emit the
same shape". They did not. `main.py:118` calls `configure_json_logging()`;
`worker.py` called `logging.basicConfig(format="%(asctime)s %(levelname)s
%(name)s %(message)s")` instead — so the one tier where all ingestion runs,
and where nearly every observed production failure happens, was the one
tier whose logs could not be filtered by level, workspace, run or trace.

Measured against workspace-georag4ad7 on 2026-08-21, trailing 24h:

    ContainerAppConsoleLogs_CL
    | summarize total=count(),
                json=countif(Log_s startswith '{'),
                trace=countif(Log_s has 'trace_id')
      by ContainerAppName_s

    fastapi-cc           total=53438  json=53380  trace=18527
    hatchet-worker-cc    total=23993  json=0      trace=0

Two properties are under test here, and the second matters as much as the
first:

  1. `configure_worker_logging()` installs the JSON formatter.
  2. It is called from `main()`, NOT at module import.

(2) is not style. `configure_json_logging` goes through `dictConfig`,
which REPLACES the root handler list — and this module is imported at
module scope by tests/test_pg_partman_maintenance.py. Configuring on
import would tear pytest's own capture and caplog handlers off the root
logger for every test that ran afterwards. `basicConfig` was safe in that
position only because it is a documented no-op when root already has
handlers; its replacement is not, so the call had to move.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.logging_config import JsonFormatter

WORKER_SRC = Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows" / "worker.py"
PROGRESS_SRC = Path(__file__).resolve().parents[1] / "app" / "hatchet_workflows" / "_progress.py"


@pytest.fixture
def restored_root_logging():
    """Snapshot and restore global logging state.

    `configure_worker_logging()` reconfigures the ROOT logger. Without
    this fixture the first test that calls it would leave every later
    test in the session without pytest's capture handlers.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {
        name: logging.getLogger(name).level
        for name in ("pdfminer", "azure.core", "grpc", "PIL")
    }
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


# ---------------------------------------------------------------------------
# 1. The formatter
# ---------------------------------------------------------------------------


def test_configure_worker_logging_installs_the_json_formatter(restored_root_logging):
    from app.hatchet_workflows.worker import configure_worker_logging

    configure_worker_logging()

    root = logging.getLogger()
    assert root.handlers, "dictConfig must leave at least one root handler"
    assert all(
        isinstance(h.formatter, JsonFormatter) for h in root.handlers
    ), (
        "Every root handler must format as JSON — a single text handler is "
        "enough to put unparseable lines into ContainerAppConsoleLogs_CL."
    )


def test_a_worker_log_line_parses_as_json_and_keeps_its_extras(restored_root_logging, capsys):
    from app.hatchet_workflows.worker import configure_worker_logging

    configure_worker_logging()

    logging.getLogger("georag.hatchet_worker").warning(
        "ingest failed",
        extra={"run_id": "d0d0d0d0-0000-0000-0000-000000000001", "workspace_id": "ws-1"},
    )

    out = capsys.readouterr().out.strip().splitlines()
    assert out, "nothing was written to stdout"
    payload = json.loads(out[-1])
    assert payload["message"] == "ingest failed"
    assert payload["level"] == "WARNING"
    # The whole point: these are top-level fields a KQL `parse_json(Log_s)`
    # can filter on, not substrings inside a rendered message.
    assert payload["run_id"] == "d0d0d0d0-0000-0000-0000-000000000001"
    assert payload["workspace_id"] == "ws-1"


def test_the_noisy_logger_suppression_survives_the_new_formatter(restored_root_logging):
    # The suppression loop sets levels, and dictConfig rebuilds handlers.
    # If the two were ever reordered, azure.core's http_logging_policy
    # would go back to ~9.6k INFO lines a day on this worker.
    from app.hatchet_workflows.worker import configure_worker_logging

    logging.getLogger("azure.core").setLevel(logging.NOTSET)
    logging.getLogger("pdfminer").setLevel(logging.NOTSET)

    configure_worker_logging()

    assert logging.getLogger("azure.core").level == logging.WARNING
    assert logging.getLogger("pdfminer").level == logging.WARNING


# ---------------------------------------------------------------------------
# 2. Where the call lives
# ---------------------------------------------------------------------------


def test_the_worker_no_longer_calls_basic_config():
    source = WORKER_SRC.read_text(encoding="utf-8")

    # Match the CALL, not the name — the replacement's docstring explains
    # what it replaced and says "logging.basicConfig" in prose.
    assert "logging.basicConfig(" not in source, (
        "basicConfig formats as plain text. It is also a no-op whenever root "
        "already has handlers, which is why this went unnoticed for so long."
    )
    assert "configure_json_logging(" in source


def test_logging_is_configured_from_main_not_at_import():
    source = WORKER_SRC.read_text(encoding="utf-8")

    # The call must be indented — i.e. inside a function body.
    call_lines = [
        line
        for line in source.splitlines()
        if "configure_json_logging(level=" in line
    ]
    assert call_lines, "the configure call disappeared"
    for line in call_lines:
        assert line.startswith("    "), (
            "configure_json_logging() must not run at module scope: dictConfig "
            "replaces the root handler list, and tests import this module for "
            "POOLS. Configuring on import removes pytest's capture handlers "
            f"for the rest of the session. Offending line: {line!r}"
        )

    assert "    configure_worker_logging()\n" in source, (
        "main() must call configure_worker_logging() — otherwise the worker "
        "runs with Python's last-resort WARNING-to-stderr handler."
    )


# ---------------------------------------------------------------------------
# 3. Correlation IDs on the ingest progress writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_failures_carry_the_run_id_as_a_structured_field(monkeypatch, caplog):
    """`... failed (run=%s)` in the message text is not queryable.

    The operator's query is
    `| extend p = parse_json(Log_s) | where p.run_id == '<uuid>'`,
    which needs run_id as a field.
    """
    from app.hatchet_workflows import _progress

    async def _boom():
        raise RuntimeError("pool is down")

    monkeypatch.setattr(_progress, "get_pool", _boom)

    with caplog.at_level(logging.WARNING, logger="georag.hatchet.progress"):
        await _progress.mark_heartbeat(run_id="11111111-2222-3333-4444-555555555555")

    matching = [r for r in caplog.records if "mark_heartbeat failed" in r.getMessage()]
    assert matching, f"expected a warning; got {[r.getMessage() for r in caplog.records]}"
    assert getattr(matching[0], "run_id", None) == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_progress_key_scoped_failures_carry_workspace_and_key(monkeypatch, caplog):
    from app.hatchet_workflows import _progress

    async def _boom():
        raise RuntimeError("pool is down")

    monkeypatch.setattr(_progress, "get_pool", _boom)

    with caplog.at_level(logging.WARNING, logger="georag.hatchet.progress"):
        await _progress.lookup_active_run_id(
            workspace_id="aaaaaaaa-0000-0000-0000-000000000001",
            minio_key="reports/x.pdf",
        )

    matching = [r for r in caplog.records if "lookup_active_run_id failed" in r.getMessage()]
    assert matching, f"expected a warning; got {[r.getMessage() for r in caplog.records]}"
    assert getattr(matching[0], "workspace_id", None) == "aaaaaaaa-0000-0000-0000-000000000001"
    assert getattr(matching[0], "minio_key", None) == "reports/x.pdf"


def test_every_progress_log_call_carries_structured_context():
    """A new call site added without `extra=` silently loses correlation.

    Counting is crude but it fails loudly the moment someone adds a bare
    `log.warning(...)` back into this module, which is the only way this
    regresses.
    """
    source = PROGRESS_SRC.read_text(encoding="utf-8")

    log_calls = source.count("log.warning(") + source.count("log.error(") + source.count("log.info(")
    extras = source.count("extra={")

    assert extras >= log_calls, (
        f"{log_calls} log calls but only {extras} carry extra={{...}} — "
        "a call site was added without correlation IDs."
    )
