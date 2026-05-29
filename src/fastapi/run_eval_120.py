"""One-shot eval re-run against the expanded uranium corpus (120 projects)."""
import asyncio
import sys
import uuid
from dataclasses import asdict, is_dataclass

sys.path.insert(0, '/app')
from app.services.eval.workspace_evaluator import run_workspace_evaluation


async def main():
    result = await run_workspace_evaluation(
        triggered_by="manual",
        trigger_payload={
            "reason": "post-overnight-uranium-ingest",
            "corpus_projects": 120,
        },
        question_set_filter="core_chat",
        blocks_promotion=False,
        eval_request_id=uuid.uuid4(),
        evaluator_kind="real_rag_v1",
    )
    print("==== EVAL RESULT ====")
    if hasattr(result, 'model_dump'):
        d = result.model_dump()
    elif is_dataclass(result):
        d = asdict(result)
    else:
        d = {k: getattr(result, k) for k in dir(result) if not k.startswith('_') and not callable(getattr(result, k))}
    for k, v in d.items():
        print(f"  {k}: {v}")


asyncio.run(main())
