"""A learning-loop or notification workflow that fails must FAIL the run.

Five workflows caught every exception and returned ``success=False`` (and
``external_notification`` logged a failed audit write and reported the
notification as recorded). Hatchet records a returned value as a COMPLETED task,
so the run list showed green, the declared ``retries`` could never fire and
nothing alerted. They re-raise now, after the best-effort failure broadcast where
the workflow had one (Hatchet audit 2026-10, finding 17).

The two that write more than one row also keep those rows together, because a
failure that now raises is a failure that gets retried or re-run:

* ``field_outcome_learning`` (``retries=1``) appends a backtest row per model
  version plus a lesson; they are one transaction, so the retry starts from
  nothing instead of writing the first version's row twice.
* ``train_target_model`` inserts the new version with ``is_active =
  activate_on_success`` and then flips every other version off; a failure
  between the two would leave two active versions of one model.

No database: the connection is scripted, so these run in the unit tier.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.hatchet_workflows import continuous_learning_loop as cll
from app.hatchet_workflows import external_notification as en
from app.hatchet_workflows import field_outcome_learning as fol
from app.hatchet_workflows import train_source_trust as tst
from app.hatchet_workflows import train_target_model as ttm


# ---------------------------------------------------------------------------
# A scripted asyncpg connection
# ---------------------------------------------------------------------------
class _Tx:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> None:
        self._conn.tx_depth += 1
        self._conn.events.append("begin")

    async def __aexit__(self, exc_type: object, *_rest: object) -> bool:
        self._conn.tx_depth -= 1
        self._conn.events.append("rollback" if exc_type else "commit")
        return False


class _Conn:
    """Enough of ``asyncpg.Connection`` for the workflow bodies.

    ``script`` maps a substring of the SQL to what the statement returns: an
    exception instance is raised, a callable is called (so it can hand out
    successive values or raise on the Nth), anything else is returned. A
    statement that matches nothing returns an empty default.
    """

    def __init__(self, script: dict[str, object] | None = None) -> None:
        self.script = script or {}
        self.tx_depth = 0
        self.events: list[str] = []
        self.writes: list[tuple[str, bool]] = []  # (sql, ran inside a transaction)
        self.closed = False

    def is_in_transaction(self) -> bool:
        return self.tx_depth > 0

    def transaction(self) -> _Tx:
        return _Tx(self)

    def _run(self, sql: str, default: object) -> object:
        for needle, result in self.script.items():
            if needle in sql:
                if isinstance(result, BaseException):
                    raise result
                return result() if callable(result) else result
        return default

    async def execute(self, sql: str, *_args: object) -> object:
        self.writes.append((sql, self.tx_depth > 0))
        return self._run(sql, "OK")

    async def fetch(self, sql: str, *_args: object) -> object:
        return self._run(sql, [])

    async def fetchval(self, sql: str, *_args: object) -> object:
        if "INSERT" in sql:
            self.writes.append((sql, self.tx_depth > 0))
        return self._run(sql, None)

    async def fetchrow(self, sql: str, *_args: object) -> object:
        return self._run(sql, None)

    async def close(self) -> None:
        self.closed = True


def _connecting(conn: _Conn):
    async def _connect(*_a: object, **_k: object) -> _Conn:
        return conn

    return _connect


@pytest.fixture
def broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture the admin-surface pushes (`laravel_bridge` is imported inside the
    workflow bodies, so patching the module attribute is enough)."""
    from app.services import laravel_bridge

    sent: list[dict] = []

    async def _post(**kwargs: object) -> None:
        sent.append(dict(kwargs))

    monkeypatch.setattr(laravel_bridge, "post_admin_surface_updated", _post)
    return sent


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture ledger anchors; `emit_audit` is imported inside the bodies."""
    from app import audit as audit_mod

    emitted: list[dict] = []

    async def _emit(conn: _Conn, **kwargs: object) -> None:
        emitted.append({**kwargs, "tx_depth_at_emit": conn.tx_depth})

    monkeypatch.setattr(audit_mod, "emit_audit", _emit)
    return emitted


# ---------------------------------------------------------------------------
# continuous_learning_loop
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_continuous_learning_loop_fails_the_run(monkeypatch) -> None:
    conn = _Conn({"silver.workspaces": RuntimeError("workspace list unreadable")})
    monkeypatch.setattr(cll.asyncpg, "connect", _connecting(conn))

    with pytest.raises(RuntimeError, match="workspace list unreadable"):
        await cll.execute.aio_mock_run(cll.ContinuousLearningLoopInput.model_validate({}))

    assert conn.closed, "the connection must be released on the failure path too"


# ---------------------------------------------------------------------------
# train_source_trust
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_train_source_trust_fails_the_run_after_broadcasting(
    monkeypatch, broadcasts,
) -> None:
    conn = _Conn({"FROM silver.reports": RuntimeError("reports unreadable")})
    monkeypatch.setattr(tst.asyncpg, "connect", _connecting(conn))

    with pytest.raises(RuntimeError, match="reports unreadable"):
        await tst.execute.aio_mock_run(
            tst.TrainSourceTrustInput(workspace_id=uuid4(), initiated_by_user_id=1),
        )

    # The operator still sees the row appear with status=failure ...
    assert [b["surface"] for b in broadcasts] == ["workflow-runs", "ml-training"]
    assert all(b["payload"]["status"] == "failure" for b in broadcasts)
    assert "reports unreadable" in broadcasts[0]["payload"]["failure_reason"]
    # ... and the connection is released.
    assert conn.closed


@pytest.mark.asyncio
async def test_a_failing_broadcast_does_not_mask_the_real_error(
    monkeypatch,
) -> None:
    """The broadcast is best effort: if Laravel is down the original exception is
    still the one that fails the run."""
    from app.services import laravel_bridge

    async def _down(**_k: object) -> None:
        raise ConnectionError("laravel unreachable")

    monkeypatch.setattr(laravel_bridge, "post_admin_surface_updated", _down)
    conn = _Conn({"FROM silver.reports": RuntimeError("reports unreadable")})
    monkeypatch.setattr(tst.asyncpg, "connect", _connecting(conn))

    with pytest.raises(RuntimeError, match="reports unreadable"):
        await tst.execute.aio_mock_run(
            tst.TrainSourceTrustInput(workspace_id=uuid4(), initiated_by_user_id=1),
        )


# ---------------------------------------------------------------------------
# train_target_model
# ---------------------------------------------------------------------------
def _train_input(**overrides: object) -> ttm.TrainTargetModelInput:
    return ttm.TrainTargetModelInput(
        target_model_id=uuid4(), initiated_by_user_id=1, **overrides,
    )


@pytest.mark.asyncio
async def test_train_target_model_fails_the_run_after_broadcasting(
    monkeypatch, broadcasts,
) -> None:
    conn = _Conn({"FROM targeting.target_outcomes": RuntimeError("outcomes unreadable")})
    monkeypatch.setattr(ttm.asyncpg, "connect", _connecting(conn))

    with pytest.raises(RuntimeError, match="outcomes unreadable"):
        await ttm.execute.aio_mock_run(_train_input())

    assert [b["surface"] for b in broadcasts] == ["workflow-runs", "ml-training"]
    assert all(b["payload"]["status"] == "failure" for b in broadcasts)
    assert conn.closed


@pytest.mark.asyncio
async def test_activation_failure_leaves_no_second_active_version(
    monkeypatch, broadcasts, audits,
) -> None:
    """The INSERT carries is_active=true; if the UPDATE that switches the other
    versions off fails, the INSERT must not survive it."""
    conn = _Conn({
        "SELECT COALESCE(max(version), 0) + 1": 3,
        "UPDATE targeting.target_model_versions": RuntimeError("deactivate failed"),
    })
    monkeypatch.setattr(ttm.asyncpg, "connect", _connecting(conn))

    with pytest.raises(RuntimeError, match="deactivate failed"):
        await ttm.execute.aio_mock_run(_train_input(activate_on_success=True))

    assert conn.events == ["begin", "rollback"]
    inserts = [in_tx for sql, in_tx in conn.writes if "INSERT INTO targeting.target_model_versions" in sql]
    assert inserts == [True], "the new version was written outside the transaction"
    assert audits == [], "a failed run must not anchor a trained model"


@pytest.mark.asyncio
async def test_activation_commits_the_insert_and_the_flip_together(
    monkeypatch, broadcasts, audits,
) -> None:
    conn = _Conn({"SELECT COALESCE(max(version), 0) + 1": 3})
    monkeypatch.setattr(ttm.asyncpg, "connect", _connecting(conn))

    out = await ttm.execute.aio_mock_run(_train_input(activate_on_success=True))

    assert out.success is True and out.activated is True
    assert conn.events == ["begin", "commit"]
    in_tx = {
        ("INSERT" if "INSERT" in sql else "UPDATE"): inside
        for sql, inside in conn.writes
        if "targeting.target_model_versions" in sql
    }
    assert in_tx == {"INSERT": True, "UPDATE": True}
    # The anchor is written after the commit.
    assert [a["tx_depth_at_emit"] for a in audits] == [0]


# ---------------------------------------------------------------------------
# field_outcome_learning
# ---------------------------------------------------------------------------
def _outcome(recommendation: str, verdict: str) -> dict:
    return {
        "outcome_id": str(uuid4()),
        "recommendation_id": recommendation,
        "hit_or_miss": verdict,
        "outcome_payload": {},
        "recorded_at": datetime(2026, 9, 1, tzinfo=UTC),
    }


def _fol_script(second_insert) -> dict[str, object]:
    """Two outcomes whose recommendations were scored by two model versions, so
    the run writes two backtest rows (the second is the one that can fail)."""
    rec_a, rec_b = str(uuid4()), str(uuid4())
    ids = iter(["bt-1"])

    def _insert() -> object:
        try:
            return next(ids)
        except StopIteration:
            return second_insert()

    return {
        "FROM targeting.target_outcomes o": [
            _outcome(rec_a, "hit"), _outcome(rec_b, "miss"),
        ],
        "JOIN targeting.target_scores s": [
            {"recommendation_id": rec_a, "model_version_id": str(uuid4())},
            {"recommendation_id": rec_b, "model_version_id": str(uuid4())},
        ],
        "INSERT INTO targeting.target_backtests": _insert,
        "FROM silver.decision_records": {"decision_id": str(uuid4())},
    }


def _fol_input() -> fol.FieldOutcomeLearningInput:
    return fol.FieldOutcomeLearningInput(workspace_id=uuid4(), project_id=uuid4())


def _boom() -> object:
    raise RuntimeError("second backtest rejected")


@pytest.mark.asyncio
async def test_field_outcome_learning_fails_the_run_and_rolls_back_its_rows(
    monkeypatch, broadcasts, audits,
) -> None:
    conn = _Conn(_fol_script(_boom))
    monkeypatch.setattr(fol.asyncpg, "connect", _connecting(conn))

    with pytest.raises(RuntimeError, match="second backtest rejected"):
        await fol.execute.aio_mock_run(_fol_input())

    # The first model version's row was written, then the run died: with retries=1
    # the retry would write it again unless it was rolled back with the rest.
    backtest_writes = [
        in_tx for sql, in_tx in conn.writes if "target_backtests" in sql
    ]
    assert backtest_writes == [True, True]
    assert conn.events == ["begin", "rollback"]
    assert audits == [] and broadcasts == []
    assert conn.closed


@pytest.mark.asyncio
async def test_field_outcome_learning_commits_backtests_and_lesson_together(
    monkeypatch, broadcasts, audits,
) -> None:
    conn = _Conn(_fol_script(lambda: "bt-2"))
    monkeypatch.setattr(fol.asyncpg, "connect", _connecting(conn))

    out = await fol.execute.aio_mock_run(_fol_input())

    assert out.success is True
    assert out.backtests_written == 2 and out.lessons_written == 1
    assert conn.events == ["begin", "commit"]
    assert all(in_tx for _sql, in_tx in conn.writes if "decision_lessons_learned" in _sql)
    # The anchor is best effort and written after the commit, so a failure to
    # write it cannot undo the rows.
    assert [a["tx_depth_at_emit"] for a in audits] == [0]


# ---------------------------------------------------------------------------
# external_notification
# ---------------------------------------------------------------------------
class _Pool:
    def __init__(self) -> None:
        self.closed = False

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


def _wire_notification(monkeypatch, *, emit) -> SimpleNamespace:
    """Everything `receive` does before it reaches the audit write is allowed
    through: sender verified, flag on, not seen before (until `state.recorded`)."""
    state = SimpleNamespace(recorded=None, pools=[])

    async def _create_pool(*_a: object, **_k: object) -> _Pool:
        pool = _Pool()
        state.pools.append(pool)
        return pool

    async def _no_rate_limit(_conn: object, _source: str) -> None:
        return None

    async def _verified(_conn: object, _input: object) -> tuple[bool, None]:
        return True, None

    async def _enabled(_conn: object) -> bool:
        return True

    async def _already_recorded(_conn: object, _notification_id: str) -> str | None:
        return state.recorded

    monkeypatch.setattr(en.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(en, "_lookup_sender_rate_limit", _no_rate_limit)
    monkeypatch.setattr(en, "verify_hmac_signature_async", _verified)
    monkeypatch.setattr(en, "_flag_enabled", _enabled)
    monkeypatch.setattr(en, "_already_recorded", _already_recorded)
    monkeypatch.setattr(en, "emit_audit", emit)
    return state


def _notification() -> en.ExternalNotificationInput:
    return en.ExternalNotificationInput(
        notification_id=f"n-{uuid4()}", source="partner-x", kind="report_filed",
        payload={"report": 7},
    )


@pytest.mark.asyncio
async def test_a_failed_audit_write_fails_the_run_instead_of_reporting_recorded(
    monkeypatch,
) -> None:
    async def _emit(_conn: object, **_k: object) -> None:
        raise RuntimeError("chain head lock timeout")

    state = _wire_notification(monkeypatch, emit=_emit)

    with pytest.raises(RuntimeError, match="chain head lock timeout"):
        await en.receive.aio_mock_run(_notification())

    assert all(p.closed for p in state.pools), "pools must be closed on the failure path"


@pytest.mark.asyncio
async def test_the_retry_of_a_failed_write_that_landed_is_a_duplicate_not_a_second_row(
    monkeypatch,
) -> None:
    """retries=1 re-runs the task. If the first attempt's row did commit (the
    error was the client losing the response), the re-run must find it."""
    calls: list[str] = []

    async def _emit(_conn: object, **kwargs: object) -> None:
        calls.append(kwargs["payload"]["notification_id"])
        raise RuntimeError("connection lost")

    state = _wire_notification(monkeypatch, emit=_emit)
    notification = _notification()

    with pytest.raises(RuntimeError, match="connection lost"):
        await en.receive.aio_mock_run(notification)

    state.recorded = "audit-1"  # the row did land
    out = await en.receive.aio_mock_run(notification)

    assert out.skipped is True and out.reason == "duplicate notification_id"
    assert out.audit_id == "audit-1"
    assert calls == [notification.notification_id], "the re-run must not write again"


@pytest.mark.asyncio
async def test_a_recorded_notification_reports_the_audit_row_it_wrote(
    monkeypatch,
) -> None:
    async def _emit(_conn: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(id="audit-9")

    _wire_notification(monkeypatch, emit=_emit)

    out = await en.receive.aio_mock_run(_notification())

    assert out.skipped is False
    assert out.audit_id == "audit-9"
