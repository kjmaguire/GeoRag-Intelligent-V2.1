"""One-shot eval re-run against the 120-project uranium corpus.

Run inside the FastAPI container:

    docker exec georag-fastapi python /app/scripts/run_eval_120.py

Used as the canonical re-baseline after model swaps (embedder or
reranker) — produces a fresh NDCG/Recall snapshot from the core_chat
question set against the real_rag_v1 evaluator. Commit the printed
result block into ``ops/baselines/qwen3-reranker-2026-06-04.md`` (or
similar dated baseline file) so the next model swap has a numerical
predecessor to diff against.

Recovered to the canonical path 2026-06-04 from worktree clones that
held the only copy. Kyle's audit reference `scripts/run_eval_120.py`
resolves to this file.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import asdict, is_dataclass

sys.path.insert(0, "/app")
from app.services.eval.workspace_evaluator import run_workspace_evaluation


async def main() -> None:
    result = await run_workspace_evaluation(
        triggered_by="manual",
        trigger_payload={
            "reason": "qwen3-reranker-rebaseline-2026-06-04",
            "corpus_projects": 120,
            "embedder": "Qwen/Qwen3-Embedding-0.6B",
            "reranker": "Qwen/Qwen3-Reranker-0.6B",
        },
        question_set_filter="core_chat",
        blocks_promotion=False,
        eval_request_id=uuid.uuid4(),
        evaluator_kind="real_rag_v1",
    )
    print("==== EVAL RESULT (Qwen3-Embedding + Qwen3-Reranker swap) ====")
    if hasattr(result, "model_dump"):
        d = result.model_dump()
    elif is_dataclass(result):
        d = asdict(result)
    else:
        d = {
            k: getattr(result, k)
            for k in dir(result)
            if not k.startswith("_") and not callable(getattr(result, k))
        }
    for k, v in d.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
