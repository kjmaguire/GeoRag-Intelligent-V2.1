"""Plan §5b — golden-query harness foundation.

A pure-Python evaluation harness for the context-preparation pipeline.
Given a list of :class:`GoldenQuery` records (each carrying expected
criteria) and a packet-producing callable, the harness:

  1. Runs the callable for each query
  2. Evaluates each ``EvaluationCriterion`` against the produced packet
  3. Returns an aggregated :class:`EvaluationReport`

The harness is deliberately separate from the Hatchet workflow
``eval_real_rag_nightly`` — that's the in-cluster, live-corpus eval
that needs real PostgreSQL + Qdrant + LLM. THIS harness runs offline,
against the pure-function context-prep pipeline, using mocked or
deterministic packet inputs. Use case: lock the diversity quotas /
authority ranking behaviour before flipping the
``CONTEXT_PREP_ENABLED`` flag on for live traffic.

Wiring: a pytest test (or CI job) constructs a callable that returns
a packet for a given query, loads golden queries from JSON, calls
:func:`run_golden_harness`, and asserts the report meets a pass-rate
threshold. The pattern keeps the eval code in the same repo as the
implementation it's evaluating — no out-of-process YAML scoring.

Pure module: no I/O except :func:`load_golden_queries` (json.load on
a path). The core eval logic is side-effect-free.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol

from app.agent.evidence import EvidencePacket


logger = logging.getLogger(__name__)


__all__ = [
    "EvaluationCriterion",
    "GoldenQuery",
    "CriterionResult",
    "QueryEvaluation",
    "EvaluationReport",
    "evaluate_packet",
    "run_golden_harness",
    "load_golden_queries",
]


# ---------------------------------------------------------------------------
# Criterion shapes
# ---------------------------------------------------------------------------


CriterionKind = Literal[
    "contains_kind",
    "min_kind_count",
    "max_kind_count",
    "exact_kinds",
    "first_kind_is",
    "first_document_type_matches",
    "min_evidence_total",
    "max_evidence_total",
    "budget_reached",
    "first_authority_rank_le",
    "evidence_id_present",
]


@dataclass(frozen=True)
class EvaluationCriterion:
    """One assertion about a produced EvidencePacket.

    Attributes:
        kind: Which check to run (see :data:`CriterionKind`).
        value: The expected value. Shape depends on ``kind``:
            - contains_kind / first_kind_is: str (e.g. "document")
            - min_kind_count / max_kind_count: dict {kind: int}
              OR (kind, n) tuple
            - exact_kinds: set/list of kind names — packet.evidence
              must contain at least one of EACH listed kind and no
              others
            - first_document_type_matches: str (substring match,
              case-insensitive)
            - min_evidence_total / max_evidence_total: int
            - budget_reached: bool
            - first_authority_rank_le: int
            - evidence_id_present: str (an evidence_id that must
              appear in the packet)
        description: Optional human-readable label for reports.
    """

    kind: CriterionKind
    value: Any
    description: str = ""


@dataclass(frozen=True)
class GoldenQuery:
    """One golden test case.

    The harness doesn't run the query through retrieval — it expects
    the caller to supply a ``packet_factory`` that maps a query string
    to a packet (for unit-test-time eval, this is typically a deterministic
    fixture; at live-eval time it's a real pipeline call wrapped in an
    async helper).
    """

    query_id: str
    query_text: str
    intent: str | None = None
    criteria: tuple[EvaluationCriterion, ...] = ()
    description: str = ""
    tags: tuple[str, ...] = ()  # e.g. ("synthesis", "spatial", "regression")


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionResult:
    """Outcome of evaluating one criterion against one packet."""

    criterion: EvaluationCriterion
    passed: bool
    actual: Any
    message: str = ""


@dataclass(frozen=True)
class QueryEvaluation:
    """All criteria evaluated for one golden query."""

    golden: GoldenQuery
    results: tuple[CriterionResult, ...]
    packet_summary: dict[str, Any]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed_criteria(self) -> tuple[CriterionResult, ...]:
        return tuple(r for r in self.results if not r.passed)


@dataclass(frozen=True)
class EvaluationReport:
    """Aggregated outcome over all golden queries."""

    evaluations: tuple[QueryEvaluation, ...]

    @property
    def total(self) -> int:
        return len(self.evaluations)

    @property
    def passed_count(self) -> int:
        return sum(1 for ev in self.evaluations if ev.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 1.0
        return self.passed_count / self.total

    def failed_queries(self) -> tuple[QueryEvaluation, ...]:
        return tuple(ev for ev in self.evaluations if not ev.passed)

    def summary(self) -> str:
        """One-line human-readable summary."""
        return (
            f"{self.passed_count}/{self.total} passed "
            f"({self.pass_rate:.1%})"
        )


# ---------------------------------------------------------------------------
# Packet factory protocol
# ---------------------------------------------------------------------------


class PacketFactory(Protocol):
    """Anything callable that maps a :class:`GoldenQuery` to a packet
    (or returns None to skip the query — e.g. when the test fixture
    has no data for it). Signature is sync so the harness composes
    with both unit tests (synchronous fixtures) and async wrappers
    (use ``asyncio.run`` inside the factory)."""

    def __call__(self, golden: GoldenQuery) -> EvidencePacket | None: ...


# ---------------------------------------------------------------------------
# Single-criterion evaluator
# ---------------------------------------------------------------------------


def _eval_criterion(
    criterion: EvaluationCriterion,
    packet: EvidencePacket,
) -> CriterionResult:
    """Evaluate ONE criterion against the packet."""
    kind = criterion.kind
    val = criterion.value
    kinds_in_packet = [e.kind for e in packet.evidence]

    if kind == "contains_kind":
        passed = val in kinds_in_packet
        return CriterionResult(
            criterion=criterion,
            passed=passed,
            actual=kinds_in_packet,
            message=f"expected '{val}' in evidence kinds {kinds_in_packet}",
        )

    if kind == "min_kind_count":
        # value is either dict {kind: n} or (kind, n)
        if isinstance(val, dict):
            for k, n in val.items():
                count = kinds_in_packet.count(k)
                if count < n:
                    return CriterionResult(
                        criterion=criterion, passed=False,
                        actual={k: kinds_in_packet.count(k) for k in val.keys()},
                        message=f"expected at least {n} {k}, got {count}",
                    )
            return CriterionResult(
                criterion=criterion, passed=True,
                actual={k: kinds_in_packet.count(k) for k in val.keys()},
            )
        # tuple shape (kind, n)
        k, n = val
        count = kinds_in_packet.count(k)
        return CriterionResult(
            criterion=criterion, passed=count >= n,
            actual=count,
            message=f"expected at least {n} {k}, got {count}",
        )

    if kind == "max_kind_count":
        if isinstance(val, dict):
            for k, n in val.items():
                count = kinds_in_packet.count(k)
                if count > n:
                    return CriterionResult(
                        criterion=criterion, passed=False,
                        actual={k: kinds_in_packet.count(k) for k in val.keys()},
                        message=f"expected at most {n} {k}, got {count}",
                    )
            return CriterionResult(
                criterion=criterion, passed=True,
                actual={k: kinds_in_packet.count(k) for k in val.keys()},
            )
        k, n = val
        count = kinds_in_packet.count(k)
        return CriterionResult(
            criterion=criterion, passed=count <= n,
            actual=count,
            message=f"expected at most {n} {k}, got {count}",
        )

    if kind == "exact_kinds":
        expected = set(val)
        actual = set(kinds_in_packet)
        return CriterionResult(
            criterion=criterion,
            passed=actual == expected,
            actual=sorted(actual),
            message=f"expected exactly {sorted(expected)}, got {sorted(actual)}",
        )

    if kind == "first_kind_is":
        first = kinds_in_packet[0] if kinds_in_packet else None
        return CriterionResult(
            criterion=criterion,
            passed=first == val,
            actual=first,
            message=f"expected first kind '{val}', got '{first}'",
        )

    if kind == "first_document_type_matches":
        # Look at the FIRST DocumentEvidence in packet order.
        from app.agent.evidence import DocumentEvidence  # noqa: PLC0415
        first_doc = next(
            (e for e in packet.evidence if isinstance(e, DocumentEvidence)),
            None,
        )
        if first_doc is None:
            return CriterionResult(
                criterion=criterion, passed=False, actual=None,
                message="no DocumentEvidence in packet",
            )
        actual_type = first_doc.document_type or ""
        passed = str(val).lower() in actual_type.lower()
        return CriterionResult(
            criterion=criterion, passed=passed,
            actual=actual_type,
            message=f"first document_type='{actual_type}', expected to contain '{val}'",
        )

    if kind == "min_evidence_total":
        return CriterionResult(
            criterion=criterion, passed=len(packet.evidence) >= int(val),
            actual=len(packet.evidence),
            message=f"expected ≥ {val} evidence entries, got {len(packet.evidence)}",
        )

    if kind == "max_evidence_total":
        return CriterionResult(
            criterion=criterion, passed=len(packet.evidence) <= int(val),
            actual=len(packet.evidence),
            message=f"expected ≤ {val} evidence entries, got {len(packet.evidence)}",
        )

    if kind == "budget_reached":
        actual = packet.remaining_budget >= 0
        return CriterionResult(
            criterion=criterion, passed=actual == bool(val),
            actual=actual,
            message=(
                f"expected budget_reached={bool(val)}, "
                f"remaining_budget={packet.remaining_budget}"
            ),
        )

    if kind == "first_authority_rank_le":
        from app.agent.evidence import DocumentEvidence  # noqa: PLC0415
        first_doc = next(
            (e for e in packet.evidence if isinstance(e, DocumentEvidence)),
            None,
        )
        if first_doc is None:
            return CriterionResult(
                criterion=criterion, passed=False, actual=None,
                message="no DocumentEvidence in packet",
            )
        rank = first_doc.authority_rank
        return CriterionResult(
            criterion=criterion, passed=rank <= int(val),
            actual=rank,
            message=f"first doc authority_rank={rank}, expected ≤ {val}",
        )

    if kind == "evidence_id_present":
        ids = {e.evidence_id for e in packet.evidence}
        return CriterionResult(
            criterion=criterion, passed=val in ids,
            actual=sorted(ids),
            message=f"expected evidence_id '{val}' in packet",
        )

    return CriterionResult(
        criterion=criterion, passed=False, actual=None,
        message=f"unknown criterion kind: {kind!r}",
    )


# ---------------------------------------------------------------------------
# Public eval surface
# ---------------------------------------------------------------------------


def evaluate_packet(
    golden: GoldenQuery,
    packet: EvidencePacket,
) -> QueryEvaluation:
    """Evaluate all of a golden query's criteria against one packet."""
    results = tuple(_eval_criterion(c, packet) for c in golden.criteria)
    summary = {
        "evidence_total": len(packet.evidence),
        "kinds": [e.kind for e in packet.evidence],
        "remaining_budget": packet.remaining_budget,
        "total_tokens": packet.total_tokens,
    }
    return QueryEvaluation(
        golden=golden,
        results=results,
        packet_summary=summary,
    )


def run_golden_harness(
    queries: Iterable[GoldenQuery],
    packet_factory: PacketFactory | Callable[[GoldenQuery], EvidencePacket | None],
    *,
    skip_when_no_packet: bool = True,
) -> EvaluationReport:
    """Run the harness over a set of golden queries.

    Args:
        queries: Iterable of GoldenQuery records.
        packet_factory: Callable returning a packet (or None) for each
            query. Synchronous — wrap async calls with ``asyncio.run``
            inside the factory if needed.
        skip_when_no_packet: When the factory returns None, skip the
            query (don't count as pass or fail). False to treat as
            an automatic fail (more conservative).

    Returns:
        :class:`EvaluationReport` with per-query results aggregated.
    """
    evals: list[QueryEvaluation] = []
    for q in queries:
        packet = packet_factory(q)
        if packet is None:
            if skip_when_no_packet:
                logger.debug(
                    "run_golden_harness: skipping %s (no packet)", q.query_id,
                )
                continue
            # Auto-fail: an empty placeholder packet with explicit fail.
            empty = EvidencePacket(
                query_id=q.query_id,
                query_text=q.query_text or " ",
                evidence=[],
                total_tokens=0,
                remaining_budget=0,
            )
            evals.append(
                QueryEvaluation(
                    golden=q,
                    results=(
                        CriterionResult(
                            criterion=EvaluationCriterion(
                                kind="min_evidence_total",
                                value=1,
                                description="packet_factory returned None",
                            ),
                            passed=False,
                            actual=0,
                            message="packet_factory returned None for this query",
                        ),
                    ),
                    packet_summary={"evidence_total": 0, "kinds": []},
                )
            )
            continue
        evals.append(evaluate_packet(q, packet))
    return EvaluationReport(evaluations=tuple(evals))


# ---------------------------------------------------------------------------
# JSON loader
# ---------------------------------------------------------------------------


def load_golden_queries(path: str | Path) -> list[GoldenQuery]:
    """Load golden queries from a JSON file.

    Expected JSON shape:

      [
        {
          "query_id": "Q1",
          "query_text": "...",
          "intent": "synthesis",
          "criteria": [
            {"kind": "contains_kind", "value": "document"},
            {"kind": "min_kind_count", "value": {"document": 2, "spatial": 1}},
            ...
          ],
          "tags": ["synthesis", "regression"]
        },
        ...
      ]

    Returns:
        List of GoldenQuery records, in JSON order. Unknown / malformed
        criterion entries are skipped with a warning.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(
            f"golden-query file at {path} must be a JSON array of objects"
        )
    out: list[GoldenQuery] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning(
                "load_golden_queries: entry %d is not an object; skipped", i,
            )
            continue
        try:
            criteria = tuple(
                EvaluationCriterion(
                    kind=c["kind"],
                    value=c["value"],
                    description=c.get("description", ""),
                )
                for c in (entry.get("criteria") or [])
                if isinstance(c, dict) and "kind" in c and "value" in c
            )
            out.append(
                GoldenQuery(
                    query_id=str(entry["query_id"]),
                    query_text=str(entry["query_text"]),
                    intent=entry.get("intent"),
                    criteria=criteria,
                    description=entry.get("description", ""),
                    tags=tuple(entry.get("tags") or ()),
                )
            )
        except KeyError as missing:
            logger.warning(
                "load_golden_queries: entry %d missing required key %s; skipped",
                i, missing,
            )
            continue
    return out
