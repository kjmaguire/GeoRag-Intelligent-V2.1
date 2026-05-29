"""One-shot eval run against the active core_chat question set.

Triggered manually post-overnight-ingest to validate that the 117 new
Wyoming roll-front uranium projects are reachable by the §04i 6-layer
RAG chain (Layer 4 entity resolution in particular).
"""
import asyncio
import json
import sys
from uuid import uuid4

sys.path.insert(0, "/app")

from app.services.eval.workspace_evaluator import run_workspace_evaluation


async def main():
    res = await run_workspace_evaluation(
        triggered_by="manual",
        trigger_payload={
            "source": "phase5-postingest-eval",
            "corpus_size_projects": 120,
            "wyoming_uranium_active": True,
        },
        question_set_filter="core_chat",
        blocks_promotion=False,
        eval_request_id=uuid4(),
        evaluator_kind="real_rag_v1",
    )
    print(json.dumps({
        "run_id": str(res.run_id),
        "question_count": res.question_count,
        "pass_count": res.pass_count,
        "fail_count": res.fail_count,
        "regression_count": res.regression_count,
        "promotion_blocked": res.promotion_blocked,
        "failure_summary": res.failure_summary,
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
