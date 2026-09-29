"""Wire-contract probe for the Cohere models on Amazon Bedrock (ADR-0022).

Since ADR-0023 (2026-09-15) only TWO of them are served from Bedrock in
production: Embed v4 and Rerank 3.5. Command A+ chat is still probed here
when BEDROCK_CHAT_MODEL_ID names a Marketplace endpoint an operator chose to
deploy; Parse 5 is probed by ops/validation/cohere_probe.py only -- the Parse
section that used to live here (on the retired object-form ``image_url``,
known to 400) was deleted 2026-09-29 (VEN-18).

**Nothing in the Bedrock adapters has been verified against a live endpoint.**
They were written to the documented contract from a session that could not
reach AWS, and every one of them says so at the top. This script is the gate:
run it, read the report, and correct the adapters from evidence before any of
them carries real traffic.

That is not a formality. The path this replaced had three behaviours confirmed
by a single live call on 2026-07-30 that documentation alone got WRONG — the
Foundry catalog table said "Text only" response formats for Command A+ while
Cohere's own docs listed it under Structured Outputs, and only a real request
settled it. The same class of disagreement is why this file exists.

What it records:

  1. **Availability.** Which Cohere models the target region actually offers
     serverless, and whether the two Marketplace endpoints exist and are
     InService. This is step 0 of the migration and the whole route rests on
     it. Only the chat endpoint is looked for now (see above).
  2. **Chat (Converse).** Whether `additionalModelRequestFields` carries a
     JSON `response_format` through; whether reasoning arrives as a
     `reasoningContent` content block, as a sibling field, or not at all;
     whether Cohere's `<|START_TEXT|>` / `<|END_TEXT|>` sentinels survive the
     runtime or are stripped by it. Those are behaviours (1), (2) and (3)
     from the Foundry contract, and NONE of them is assumed to carry over.
  3. **Chat streaming.** The event shape of `converse_stream` — which events
     carry text, which carry reasoning, and where usage lands — because the
     SSE path is what makes first-token latency an honest measure.
  4. **Embeddings.** That the request body really is Cohere's own v2 schema
     minus `model`, and that `output_dimension: 1024` is honoured. A silently
     ignored dimension writes 1536-dim vectors into a 1024-dim collection.
     Since 2026-09-29 also: `input_type="search_query"` (the query path), a
     96-text batch (the per-request limit _BedrockEmbedding chunks to), and
     ONE image through `images[]` then `inputs[]` -- which of the two Embed
     v4 accepts on Bedrock is undocumented and ingest depends on it.
  5. **Rerank.** The `bedrock-agent-runtime.rerank` response shape, and a
     SANITY CHECK on Rerank 3.5's scores: does an obviously-relevant document
     clear `RERANKER_SCORE_THRESHOLD_HOSTED` (0.2, measured against v4) and
     do obviously-irrelevant ones fall under it. That is four hand-written
     documents, NOT a calibration — it can catch a floor that is grossly
     wrong for 3.5 and cannot tell you the right value. Re-measuring the
     threshold properly needs chunk-level relevance labels that do not exist
     yet; see app/services/reranker.py for what that would take.
  6. Error shapes and latency, so the retry and fallback branches are tuned
     against something real -- reported against the reranker's REAL caller
     budget (reranker._caller_budget_s()), not a hard-coded 8 s.

No secrets are written to the report — Bedrock authenticates with the caller's
IAM identity, so there are none to leak, which is itself one of the things the
move bought.

Usage:
    BEDROCK_REGION=us-east-1 \\
    uv run python ops/validation/bedrock_probe.py \\
        --pdf src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf \\
        --pages 1 --out ops/validation/reports/

``--pdf``/``--pages`` now only choose the page rendered for the image-embed
variant; without them a small synthetic image is used. Set
BEDROCK_CHAT_MODEL_ID only if a Marketplace chat endpoint exists.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The wire contract lives with the adapters it describes, because it is a
# claim ABOUT them and tests/test_bedrock_wire_contract.py holds the two
# together. Importing it here is what turns this probe's output from a
# transcript into a diff.
#
# Guarded, and degrading to a skip rather than an exception, for the same
# reason every section below degrades: this script has to stay runnable from
# an operator's laptop against a checkout that may not have the FastAPI
# package importable. Losing the diff must not cost the observations.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fastapi"))

from _probe_verdict import compute_verdict  # noqa: E402
try:
    from app.services.bedrock_wire import diff_report as _diff_report
except Exception as _exc:  # noqa: BLE001 — any import failure, not just ImportError
    _DIFF_IMPORT_ERROR: str | None = f"{type(_exc).__name__}: {_exc}"

    def _diff_report(report: dict) -> dict:  # type: ignore[misc]
        return {"skipped": f"app.services.bedrock_wire unavailable ({_DIFF_IMPORT_ERROR})"}
else:
    _DIFF_IMPORT_ERROR = None

#: Embed v4's image ceiling is 2M pixels; render the probe image under it.
_EMBED_IMAGE_MAX_PIXELS = 1_900_000
#: The per-request text limit _BedrockEmbedding chunks to ([ASSUMED] vendor
#: figure until this variant runs).
_EMBED_MAX_TEXTS = 96

#: Sentinel tokens Cohere wraps JSON-mode output in. Whether the Bedrock
#: runtime strips them is exactly what step 2 answers.
SENTINELS = ("<|START_TEXT|>", "<|END_TEXT|>")


def _client(service: str):
    import boto3
    from botocore.config import Config

    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    return boto3.client(service, config=Config(region_name=region, read_timeout=180))


def _err(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        return {
            "type": type(exc).__name__,
            "code": error.get("Code"),
            "message": (error.get("Message") or "")[:400],
            "status": response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
        }
    return {"type": type(exc).__name__, "message": str(exc)[:400]}


# ---------------------------------------------------------------------------
# 1. Availability — step 0 of the migration
# ---------------------------------------------------------------------------


def probe_availability() -> dict[str, Any]:
    """What the region actually offers. The whole route rests on this."""
    out: dict[str, Any] = {}
    try:
        models = _client("bedrock").list_foundation_models()["modelSummaries"]
        out["cohere_serverless"] = sorted(
            m["modelId"] for m in models
            if m.get("providerName", "").lower().startswith("cohere")
        )
    except Exception as exc:  # noqa: BLE001
        out["cohere_serverless_error"] = _err(exc)

    out["marketplace_endpoints"] = {}
    sagemaker = _client("sagemaker")
    # Chat only: Parse left Bedrock with ADR-0023 and is probed by
    # cohere_probe.py against Cohere's own API.
    for label, env in (("chat", "BEDROCK_CHAT_MODEL_ID"),):
        arn = os.environ.get(env, "")
        name = arn.rsplit("/", 1)[-1] if arn else ""
        if not name:
            out["marketplace_endpoints"][label] = {"configured": False}
            continue
        try:
            described = sagemaker.describe_endpoint(EndpointName=name)
            out["marketplace_endpoints"][label] = {
                "configured": True,
                "name": name,
                "status": described.get("EndpointStatus"),
                "failure_reason": described.get("FailureReason"),
            }
        except Exception as exc:  # noqa: BLE001
            out["marketplace_endpoints"][label] = {
                "configured": True, "name": name, "error": _err(exc)}
    return out


# ---------------------------------------------------------------------------
# 2-3. Chat — the three Foundry behaviours, re-asked
# ---------------------------------------------------------------------------

_JSON_PROMPT = (
    "Reply with a JSON object having exactly one key, \"ok\", set to true. "
    "No prose, no code fence."
)


def probe_chat() -> dict[str, Any]:
    model_id = os.environ.get("BEDROCK_CHAT_MODEL_ID", "")
    if not model_id:
        return {"skipped": "BEDROCK_CHAT_MODEL_ID unset"}

    runtime = _client("bedrock-runtime")
    out: dict[str, Any] = {"model_id": model_id}

    request = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": _JSON_PROMPT}]}],
        "inferenceConfig": {"maxTokens": 256, "temperature": 0.0},
    }

    # (1) Does additionalModelRequestFields carry response_format through?
    #     Every typed-output guard in orchestrator_validators.py depends on
    #     the model actually returning JSON (hard rule 4).
    for label, extra in (
        ("without_response_format", {}),
        ("with_response_format",
         {"additionalModelRequestFields": {"response_format": {"type": "json_object"}}}),
    ):
        try:
            started = time.monotonic()
            response = runtime.converse(**{**request, **extra})
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            out[label] = {"error": _err(exc)}
            continue

        message = (response.get("output") or {}).get("message") or {}
        blocks = message.get("content") or []
        text = "".join(b["text"] for b in blocks if "text" in b)

        out[label] = {
            "latency_s": round(elapsed, 3),
            "stop_reason": response.get("stopReason"),
            "usage": response.get("usage"),
            # (2) Where does reasoning live? Converse's own representation is
            #     a reasoningContent block; the Foundry-era shape was a
            #     sibling `reasoning_content` field. Both are checked.
            "content_block_keys": sorted({k for b in blocks for k in b}),
            "message_sibling_keys": sorted(k for k in message if k != "content"),
            # (3) Do Cohere's sentinels survive the runtime?
            "sentinels_present": [s for s in SENTINELS if s in text],
            "text_head": text[:400],
            "parses_as_json": _parses(text),
        }
    return out


def _parses(text: str) -> bool:
    stripped = text
    for sentinel in SENTINELS:
        stripped = stripped.replace(sentinel, "")
    try:
        json.loads(stripped.strip())
    except Exception:  # noqa: BLE001
        return False
    return True


def probe_chat_stream() -> dict[str, Any]:
    """Event shape of converse_stream. Streaming is what makes first-token
    latency honest, so the delta shape has to be read rather than assumed."""
    model_id = os.environ.get("BEDROCK_CHAT_MODEL_ID", "")
    if not model_id:
        return {"skipped": "BEDROCK_CHAT_MODEL_ID unset"}

    runtime = _client("bedrock-runtime")
    try:
        started = time.monotonic()
        response = runtime.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Count to five."}]}],
            inferenceConfig={"maxTokens": 128, "temperature": 0.0},
        )
        event_kinds: list[str] = []
        delta_keys: set[str] = set()
        first_token_s: float | None = None
        usage: dict | None = None
        for event in response["stream"]:
            event_kinds.append(next(iter(event)))
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta") or {}
                delta_keys.update(delta)
                if first_token_s is None and "text" in delta:
                    first_token_s = time.monotonic() - started
            elif "metadata" in event:
                usage = event["metadata"].get("usage")
        return {
            "event_kinds_in_order": event_kinds[:20],
            "distinct_event_kinds": sorted(set(event_kinds)),
            "delta_keys": sorted(delta_keys),
            "first_token_s": round(first_token_s, 3) if first_token_s else None,
            "usage": usage,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": _err(exc)}


# ---------------------------------------------------------------------------
# 4. Embeddings
# ---------------------------------------------------------------------------


def _embed_call(runtime: Any, model_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    raw = runtime.invoke_model(
        modelId=model_id, body=json.dumps(body),
        accept="application/json", contentType="application/json")
    return json.loads(raw["body"].read()), time.monotonic() - started


def _probe_image_png(pdf: Path | None, page: int) -> tuple[bytes, str] | None:
    """One PNG under Embed v4's 2M-pixel cap: a PDF page if given, else synthetic."""
    import io as _io

    if pdf is not None and pdf.exists():
        try:
            import pypdfium2

            document = pypdfium2.PdfDocument(str(pdf))
            try:
                target = document[page - 1]
                width, height = target.get_size()
                scale = min(4.0, (_EMBED_IMAGE_MAX_PIXELS / (width * height)) ** 0.5)
                image = target.render(scale=scale).to_pil()
            finally:
                document.close()
            buffer = _io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue(), f"{pdf.name} page {page}"
        except Exception:  # noqa: BLE001 -- fall through to the synthetic image
            pass
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    image = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(image).text((40, 40), "Drill hole MAD-21-003: 12 m quartz veining", fill="black")
    buffer = _io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), "synthetic 800x600"


def probe_embed(pdf: Path | None = None, page: int = 1) -> dict[str, Any]:
    """Embed v4 on Bedrock: the document call, then three variants.

    The top-level keys stay those of the single search_document call, so a
    report from before 2026-09-29 and one from after are read by the same
    wire-contract evidence path (``top_level_keys``). Each variant is its own
    nested result, so one refused variant does not hide the others.
    """
    model_id = os.environ.get("BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4:0")
    runtime = _client("bedrock-runtime")
    body = {
        "texts": ["quartz-carbonate vein hosted gold"],
        "input_type": "search_document",
        "embedding_types": ["float"],
        "output_dimension": 1024,
    }
    try:
        payload, elapsed = _embed_call(runtime, model_id, body)
        vectors = payload["embeddings"]["float"]
        out: dict[str, Any] = {
            "model_id": model_id,
            "latency_s": round(elapsed, 3),
            "top_level_keys": sorted(payload),
            "dimension": len(vectors[0]),
            # A silently ignored output_dimension writes the wrong width into
            # a 1024-dim collection, and retrieval refuses every question.
            "dimension_honoured": len(vectors[0]) == 1024,
        }
        out["document"] = {"top_level_keys": out["top_level_keys"], "latency_s": out["latency_s"]}
    except Exception as exc:  # noqa: BLE001
        return {"model_id": model_id, "error": _err(exc)}

    # The query path: same key, the other value. tools.py sends it on every
    # question; the 2026-09-16 run never did.
    try:
        payload, elapsed = _embed_call(runtime, model_id, {**body, "input_type": "search_query"})
        vectors = payload["embeddings"]["float"]
        out["query"] = {
            "latency_s": round(elapsed, 3),
            "top_level_keys": sorted(payload),
            "dimension": len(vectors[0]),
        }
    except Exception as exc:  # noqa: BLE001
        out["query"] = {"error": _err(exc)}

    # The batch limit. _BedrockEmbedding splits at 96; if 96 is refused the
    # split point is wrong and every ingest batch fails.
    texts = [f"assay interval {i}: {0.1 * i:.1f} g/t Au over 1.5 m" for i in range(_EMBED_MAX_TEXTS)]
    try:
        payload, elapsed = _embed_call(runtime, model_id, {**body, "texts": texts})
        vectors = payload["embeddings"]["float"]
        out["batch"] = {
            "texts_sent": len(texts),
            "vectors_back": len(vectors),
            "latency_s": round(elapsed, 3),
            "top_level_keys": sorted(payload),
        }
    except Exception as exc:  # noqa: BLE001
        out["batch"] = {"texts_sent": len(texts), "error": _err(exc)}

    # ONE image, primary shape then fallback -- exactly the order
    # _BedrockEmbedding.embed_image tries them, and only on a
    # ValidationException, as it does.
    rendered = _probe_image_png(pdf, page)
    if rendered is None:
        out["image"] = {"skipped": "no pypdfium2 page and no PIL for a synthetic image"}
        return out
    png, source = rendered
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    shapes = (
        ("images", {"images": [uri]}),
        ("inputs", {"inputs": [{"content": [{"type": "image_url", "image_url": {"url": uri}}]}]}),
    )
    attempts: dict[str, Any] = {}
    for name, shape in shapes:
        image_body = {**shape, "input_type": "image", "embedding_types": ["float"], "output_dimension": 1024}
        try:
            payload, elapsed = _embed_call(runtime, model_id, image_body)
        except Exception as exc:  # noqa: BLE001
            attempts[name] = _err(exc)
            if attempts[name].get("code") == "ValidationException":
                continue
            break
        vectors = payload["embeddings"]["float"]
        out["image"] = {
            "source": source,
            "png_bytes": len(png),
            "shape_accepted": name,
            "rejected_first": attempts or None,
            "latency_s": round(elapsed, 3),
            "top_level_keys": sorted(payload),
            "dimension": len(vectors[0]),
        }
        return out
    out["image"] = {"source": source, "png_bytes": len(png), "error": attempts}
    return out


# ---------------------------------------------------------------------------
# 5. Rerank — the version regression
# ---------------------------------------------------------------------------

_RERANK_QUERY = "What is the indicated gold grade at the Madison deposit?"
_RERANK_DOCS = [
    "Indicated resources at Madison total 3.1 Mt at 1.9 g/t Au.",
    "The property is accessible by a gravel road from the highway.",
    "Drill hole MAD-21-003 intersected 12 m of quartz veining.",
    "Weather during the 2024 field season was unusually wet.",
]


def probe_rerank() -> dict[str, Any]:
    model_id = os.environ.get("BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0")
    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    arn = model_id if model_id.startswith("arn:") else (
        f"arn:aws:bedrock:{region}::foundation-model/{model_id}")

    try:
        started = time.monotonic()
        response = _client("bedrock-agent-runtime").rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": _RERANK_QUERY}}],
            sources=[
                {"type": "INLINE",
                 "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d}}}
                for d in _RERANK_DOCS
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": arn},
                    "numberOfResults": len(_RERANK_DOCS),
                },
            },
        )
        results = response["results"]
        scores = [r["relevanceScore"] for r in results]
        return {
            "model_id": model_id,
            "latency_s": round(time.monotonic() - started, 3),
            "result_keys": sorted(results[0]),
            "scores_by_index": {r["index"]: r["relevanceScore"] for r in results},
            "min": min(scores),
            "max": max(scores),
            # THE POINT OF THIS PROBE. RERANKER_SCORE_THRESHOLD_HOSTED is
            # 0.2, measured against Rerank v4. If the obviously-relevant
            # document scores below it, or the obviously-irrelevant ones
            # score above it, the system's only retrieval-quality gate is
            # mis-calibrated and every answer is affected.
            "relevant_doc_score": next(
                (r["relevanceScore"] for r in results if r["index"] == 0), None),
            "irrelevant_doc_scores": [
                r["relevanceScore"] for r in results if r["index"] in (1, 3)],
            "current_threshold": 0.2,
        }
    except Exception as exc:  # noqa: BLE001
        return {"model_id": model_id, "error": _err(exc)}


# ---------------------------------------------------------------------------
# 6. Parse -- DELETED 2026-09-29 (VEN-18)
# ---------------------------------------------------------------------------
# It probed a Bedrock Marketplace Parse endpoint that ADR-0023 retired, with
# the object-form document.image_url that Cohere refuses with HTTP 400. Parse
# is probed by cohere_probe.py, against Cohere's own API, with the string form.


# ---------------------------------------------------------------------------


def _reranker_budget() -> dict[str, Any]:
    """The budget the live reranker actually runs under, from the app itself.

    This used to be a literal 8.0 -- the pre-2026-08-20 value -- while the
    real derived budget is 19 s. Read from reranker._caller_budget_s() and
    _bedrock.retry_profile_within_budget() so the report cannot drift from
    the code again.
    """
    try:
        from app.services import _bedrock as bedrock_mod
        from app.services import reranker as reranker_mod

        budget = reranker_mod._caller_budget_s()
        read_timeout = min(reranker_mod.BEDROCK_RERANK_TIMEOUT_S, budget / 2.0)
        attempts, read_timeout = bedrock_mod.retry_profile_within_budget(
            budget, read_timeout_s=read_timeout, ceiling=reranker_mod.BEDROCK_RERANK_MAX_ATTEMPTS
        )
        return {"budget_s": round(budget, 3), "max_attempts": attempts, "read_timeout_s": round(read_timeout, 3)}
    except Exception as exc:  # noqa: BLE001 -- the latency numbers still stand without it
        return {"error": f"{type(exc).__name__}: {exc}"}


def probe_latency(samples: int) -> dict[str, Any]:
    """p50/p95 on the rerank path, which is the one with a hard budget."""
    timings: list[float] = []
    for _ in range(samples):
        started = time.monotonic()
        result = probe_rerank()
        if "error" in result:
            return {"error": result["error"], "samples_taken": len(timings)}
        timings.append(time.monotonic() - started)
    if not timings:
        return {}
    ordered = sorted(timings)
    return {
        "samples": len(ordered),
        "p50_s": round(statistics.median(ordered), 3),
        "p95_s": round(ordered[int(len(ordered) * 0.95) - 1], 3),
        # If p95 is anywhere near the per-attempt read timeout, the retry is
        # dead code again — the 2026-08-20 defect, in a new host.
        "reranker_budget": _reranker_budget(),
    }


# The sections that have to report a real observation for this run to count as
# evidence. `availability` is deliberately excluded: it can legitimately come
# back empty in a region that offers nothing, and that IS the finding.
_EVIDENCE_SECTIONS = ("chat", "chat_stream", "embed", "rerank", "latency")


#: Error codes that mean the credentials were the problem, not the call.
#: Worth saying separately from "nothing worked": one is fixed by running
#: `aws login`, the other by reading the report.
_AUTH_CODES = frozenset({
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "AccessDeniedException",
    "ExpiredTokenException",
})


def _is_auth_failure(section: dict) -> bool:
    return (section.get("error") or {}).get("code") in _AUTH_CODES


def verdict(report: dict) -> dict:
    """Say plainly whether this run verified anything.

    The reasoning lives in ops/validation/_probe_verdict.py, which both
    probes share — see that module's docstring for why a report that
    verifies nothing must not read as a pass, and what shipped once because
    it did. This function is the Bedrock half: which sections count as
    evidence, and how a credentials failure is recognised here.

    ``availability`` is checked for an auth failure too even though it is
    not an evidence section, because it is usually the FIRST call a run
    makes — so it is where an expired token shows up before anything else
    has had a chance to.
    """
    result = compute_verdict(
        report,
        sections=_EVIDENCE_SECTIONS,
        is_auth_failure=_is_auth_failure,
        auth_hint="Check credentials, region and IAM permissions.",
    )
    if not result["authentication_failed"]:
        availability_auth = (
            (report.get("availability") or {})
            .get("cohere_serverless_error", {})
            .get("code")
            in _AUTH_CODES
        )
        if availability_auth:
            result["authentication_failed"] = True
            if not result["verified_anything"]:
                result["summary"] = (
                    "could not authenticate to Bedrock; nothing was observed. "
                    "Check credentials, region and IAM permissions."
                )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=None)
    parser.add_argument("--pages", default="1")
    parser.add_argument("--latency-samples", type=int, default=5)
    parser.add_argument("--out", type=Path,
                        default=Path("ops/validation/reports"))
    args = parser.parse_args()

    pages = [int(p) for p in args.pages.split(",") if p.strip()]

    report = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "region": os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION"),
        "availability": probe_availability(),
        "chat": probe_chat(),
        "chat_stream": probe_chat_stream(),
        "embed": probe_embed(args.pdf, pages[0] if pages else 1),
        "rerank": probe_rerank(),
        "latency": probe_latency(args.latency_samples),
    }

    report["verdict"] = verdict(report)
    # Field-by-field against app/services/bedrock_wire.py. The verdict above
    # answers "did this run observe anything"; this answers "does what it
    # observed match what the adapters believe", which is a different
    # question and the one that names the file to edit.
    report["contract_diff"] = _diff_report(report)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = args.out / f"bedrock_probe_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(json.dumps(report, indent=2, default=str))
    print(f"\nreport written to {path}", file=sys.stderr)

    v = report["verdict"]
    if not v["verified_anything"]:
        print(
            f"\nPROBE FAILED — {v['summary']}\n"
            "DO NOT commit this report as evidence: it verifies nothing, and a\n"
            "report on file is exactly what ADR-0022 treats as the gate on\n"
            "trusting the adapters. Fix the access problem and re-run.",
            file=sys.stderr,
        )
        return 1

    if v["sections_failed"] or v["sections_skipped"]:
        print(f"\nPARTIAL — {v['summary']}", file=sys.stderr)

    diff = report["contract_diff"]
    broken = diff.get("required_fields_missing") or {}
    undeclared = diff.get("undeclared_fields") or {}

    if undeclared:
        # Not a failure: a field arriving that no adapter reads costs nothing
        # today. It is reported because it is the only way anyone finds out a
        # shape exists that nobody wrote down.
        print(
            "\nUNDECLARED FIELDS observed — nothing reads these, and "
            "app/services/bedrock_wire.py does not declare them:",
            file=sys.stderr,
        )
        for call, keys in sorted(undeclared.items()):
            print(f"  {call}: {', '.join(keys)}", file=sys.stderr)

    if broken:
        # This IS a failure, and a different one from "nothing was observed".
        # A required field the probe looked for and did not find means an
        # adapter reads something Bedrock does not send: the path is broken,
        # not merely unverified, and the report should not read as a pass.
        print(
            "\nCONTRACT VIOLATED — the probe observed these calls and a "
            "REQUIRED field was absent from each:",
            file=sys.stderr,
        )
        for call, keys in sorted(broken.items()):
            print(f"  {call}: missing {', '.join(keys)}", file=sys.stderr)
        print(
            "\nEach line names an adapter that reads a field Bedrock did not "
            "send. Correct the adapter AND app/services/bedrock_wire.py "
            "together — its conformance tests fail if you move only one — "
            "then re-run. Commit this report either way: a contradiction is "
            "evidence, and it is the evidence hardest to reconstruct later.",
            file=sys.stderr,
        )
        return 1

    if diff.get("skipped"):
        print(f"\nCONTRACT DIFF SKIPPED — {diff['skipped']}", file=sys.stderr)
    elif diff.get("calls_observed"):
        print(
            "\nCONTRACT HOLDS for the calls this run reached: "
            f"{', '.join(diff['calls_observed'])}. Not observed: "
            f"{', '.join(diff['calls_not_observed']) or 'none'}.",
            file=sys.stderr,
        )

    print(
        "\nCOMMIT THIS REPORT. The adapters say [UNVERIFIED] at the top until "
        "one exists, and the three Foundry behaviours it re-asks (JSON mode, "
        "where reasoning lives, sentinel tokens) were things documentation "
        "got wrong once already.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
