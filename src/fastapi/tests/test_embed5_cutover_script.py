"""scripts/ops/embed5_cutover.py: the pure decisions and the gates that must
refuse before anything destructive runs (ADR-0025 migration steps 1-6)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "embed5_cutover.py"


@pytest.fixture
def cutover(monkeypatch):
    spec = importlib.util.spec_from_file_location("embed5_cutover_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _points(**over):
    base = {"total": 10, "tagged_with_model": 10, "lacking_model_tag": 0, "images": 2, "model": "embed-v5.0-pro"}
    base.update(over)
    return base


def _passages(**over):
    base = {"total": 10, "unembedded": 0, "embedded": 10, "images": 2, "images_embedded": 2}
    base.update(over)
    return base


class TestVerdict:
    def test_complete_and_consistent_passes(self, cutover) -> None:
        rc, line = cutover.verdict(_points(), _passages())
        assert rc == cutover.EXIT_OK and "complete" in line

    def test_a_running_sweep_is_not_yet_not_an_error(self, cutover) -> None:
        rc, line = cutover.verdict(
            _points(total=4, tagged_with_model=4, images=0),
            _passages(unembedded=6, embedded=4, images_embedded=0),
        )
        assert rc == cutover.EXIT_INCOMPLETE and "6 of 10" in line

    def test_a_point_in_the_old_space_is_an_error_even_mid_sweep(self, cutover) -> None:
        """ADR-0025 gotcha 2: two vector spaces still return results, ranked by
        meaningless cosines. That is never "in progress"."""
        rc, line = cutover.verdict(
            _points(total=5, tagged_with_model=4, lacking_model_tag=1),
            _passages(unembedded=5, embedded=5),
        )
        assert rc == cutover.EXIT_ERROR and "two vector spaces" in line

    def test_orphan_points_are_an_error(self, cutover) -> None:
        rc, _ = cutover.verdict(_points(total=12, tagged_with_model=12), _passages())
        assert rc == cutover.EXIT_ERROR

    def test_embedded_image_passages_need_an_image_point(self, cutover) -> None:
        rc, line = cutover.verdict(_points(images=0), _passages())
        assert rc == cutover.EXIT_ERROR and "modality=image" in line

    def test_a_count_mismatch_at_the_end_is_an_error(self, cutover) -> None:
        rc, _ = cutover.verdict(_points(total=9, tagged_with_model=9), _passages())
        assert rc == cutover.EXIT_ERROR


class TestStreamSummary:
    def test_counts_only_no_text(self, cutover) -> None:
        raw = (
            "event: status\ndata: {}\n\n"
            'event: delta\ndata: {"text": "secret answer text"}\n\n'
            'event: delta\ndata: {"text": "more"}\n\n'
            'event: citation\ndata: {"source_chunk_id": "x"}\n\n'
            "event: completed\ndata: {}\n\n"
        )
        out = cutover.summarize_stream(cutover.parse_sse(raw))
        assert out == {
            "frames": 5, "deltas": 2, "citations": 1, "terminal": "completed",
            "refused": False, "refusal_code": None,
        }
        assert "secret" not in json.dumps(out)

    def test_a_failed_frame_is_a_refusal_with_its_code(self, cutover) -> None:
        raw = 'event: failed\ndata: {"code": "RETRIEVAL_QUALITY_GATE", "error": "no evidence"}\n\n'
        out = cutover.summarize_stream(cutover.parse_sse(raw))
        assert out["refused"] and out["refusal_code"] == "RETRIEVAL_QUALITY_GATE" and out["terminal"] == "failed"

    def test_a_completed_frame_with_a_refusal_payload_counts_as_refused(self, cutover) -> None:
        raw = 'event: completed\ndata: {"refusal_payload": {"reason": "weak_retrieval"}}\n\n'
        out = cutover.summarize_stream(cutover.parse_sse(raw))
        assert out["refused"] and out["refusal_code"] == "weak_retrieval"


class TestResetGates:
    """Every gate refuses BEFORE the reset module is even loaded."""

    @pytest.fixture(autouse=True)
    def _no_loading(self, cutover, monkeypatch):
        import importlib.util as ilu

        def _boom(*a, **k):
            raise AssertionError("the reset script must not load when a gate refuses")
        monkeypatch.setattr(ilu, "spec_from_file_location", _boom)

    async def test_refuses_unless_the_task_runs_as_cohere(self, cutover, monkeypatch) -> None:
        monkeypatch.setenv("EMBEDDING_BACKEND", "bedrock")
        rc, summary, report = await cutover.step_reset("_ops/qdrant-snapshots/x", cutover.CONFIRM_PHRASE)
        assert rc == cutover.EXIT_ERROR and "not 'cohere'" in summary
        assert report["gates"]["embedding_backend"] == "bedrock"

    async def test_refuses_without_a_snapshot(self, cutover, monkeypatch) -> None:
        monkeypatch.setenv("EMBEDDING_BACKEND", "cohere")
        monkeypatch.setattr(cutover, "_snapshot_exists", lambda prefix: (False, "no object under s3://b/" + prefix))
        rc, summary, _ = await cutover.step_reset("_ops/qdrant-snapshots/x", cutover.CONFIRM_PHRASE)
        assert rc == cutover.EXIT_ERROR and "no snapshot" in summary

    async def test_refuses_the_wrong_phrase(self, cutover, monkeypatch) -> None:
        monkeypatch.setenv("EMBEDDING_BACKEND", "cohere")
        monkeypatch.setattr(cutover, "_snapshot_exists", lambda prefix: (True, "s3://b/x"))
        rc, summary, _ = await cutover.step_reset("_ops/qdrant-snapshots/x", "yes")
        assert rc == cutover.EXIT_ERROR and "--confirm" in summary

    def test_the_snapshot_prefix_must_live_under_the_snapshot_root(self, cutover, monkeypatch) -> None:
        monkeypatch.setenv("AWS_BUCKET_BACKUPS", "b")
        ok, detail = cutover._snapshot_exists("somewhere/else")
        assert not ok and cutover.SNAPSHOT_ROOT in detail


def test_main_emits_the_markers_even_when_a_step_crashes(cutover, monkeypatch, capsys) -> None:
    async def _crash():
        raise RuntimeError("qdrant unreachable")
    monkeypatch.setattr(cutover, "step_verify", _crash)

    rc = cutover.main(["--step", "verify"])

    out = capsys.readouterr().out
    assert rc == cutover.EXIT_ERROR
    assert cutover.BEGIN_SUM in out and cutover.END_JSON in out
    body = out.split(cutover.BEGIN_JSON)[1].split(cutover.END_JSON)[0]
    report = json.loads(body)
    assert report["step"] == "verify" and report["error"]["message"] == "qdrant unreachable"
