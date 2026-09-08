"""Wire-contract probe for the four Cohere models on Amazon Bedrock (ADR-0022).

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
     it; if Command A+ or Parse 5 are absent, stop and read ADR-0022 §11.
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
  5. **Rerank.** The `bedrock-agent-runtime.rerank` response shape, and — the
     point of the exercise — the SCORE DISTRIBUTION of Rerank 3.5 against
     the same inputs v4 was calibrated on. `RERANKER_SCORE_THRESHOLD_HOSTED`
     is 0.2, measured against v4, and it is the only retrieval-quality gate
     in the system.
  6. **Parse.** The full key shape of `pages[0]` in `blocks` and `markdown`
     mode, and the pixel ladder for `COHERE_PARSE_MAX_PIXELS`. This contract
     has NEVER been verified, on Foundry or on Bedrock.
  7. Error shapes and latency, so the retry and fallback branches are tuned
     against something real.

No secrets are written to the report — Bedrock authenticates with the caller's
IAM identity, so there are none to leak, which is itself one of the things the
move bought.

Usage:
    BEDROCK_REGION=us-east-1 \\
    BEDROCK_CHAT_MODEL_ID=arn:aws:sagemaker:...:endpoint/georag-chat \\
    BEDROCK_PARSE_MODEL_ID=arn:aws:sagemaker:...:endpoint/georag-parse \\
    uv run python ops/validation/bedrock_probe.py \\
        --pdf src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf \\
        --pages 1,7 --out ops/validation/reports/
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

PIXEL_LADDER = (1_900_000, 4_000_000, 8_000_000, 12_000_000, 20_000_000)

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
    for label, env in (("chat", "BEDROCK_CHAT_MODEL_ID"),
                       ("parse", "BEDROCK_PARSE_MODEL_ID")):
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


def probe_embed() -> dict[str, Any]:
    model_id = os.environ.get("BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4:0")
    runtime = _client("bedrock-runtime")
    body = {
        "texts": ["quartz-carbonate vein hosted gold"],
        "input_type": "search_document",
        "embedding_types": ["float"],
        "output_dimension": 1024,
    }
    try:
        started = time.monotonic()
        raw = runtime.invoke_model(
            modelId=model_id, body=json.dumps(body),
            accept="application/json", contentType="application/json")
        payload = json.loads(raw["body"].read())
        vectors = payload["embeddings"]["float"]
        return {
            "model_id": model_id,
            "latency_s": round(time.monotonic() - started, 3),
            "top_level_keys": sorted(payload),
            "dimension": len(vectors[0]),
            # A silently ignored output_dimension writes the wrong width into
            # a 1024-dim collection, and retrieval refuses every question.
            "dimension_honoured": len(vectors[0]) == 1024,
        }
    except Exception as exc:  # noqa: BLE001
        return {"model_id": model_id, "error": _err(exc)}


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
# 6. Parse — never verified on any host
# ---------------------------------------------------------------------------


def _render(pdf: Path, page: int, max_pixels: int) -> bytes | None:
    try:
        import pypdfium2
    except ImportError:
        return None
    document = pypdfium2.PdfDocument(str(pdf))
    try:
        target = document[page - 1]
        width, height = target.get_size()
        scale = min(4.0, (max_pixels / (width * height)) ** 0.5)
        image = target.render(scale=scale).to_pil()
        import io as _io
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        document.close()


def probe_parse(pdf: Path | None, pages: list[int]) -> dict[str, Any]:
    model_id = os.environ.get("BEDROCK_PARSE_MODEL_ID", "")
    if not model_id:
        return {"skipped": "BEDROCK_PARSE_MODEL_ID unset"}
    if pdf is None or not pdf.exists():
        return {"skipped": "no --pdf given"}

    runtime = _client("bedrock-runtime")
    out: dict[str, Any] = {"model_id": model_id, "formats": {}, "pixel_ladder": {}}

    for output_format in ("blocks", "markdown"):
        png = _render(pdf, pages[0], 4_000_000)
        if png is None:
            out["formats"][output_format] = {"skipped": "pypdfium2 unavailable"}
            continue
        uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        body = {
            "document": {"type": "image_url", "image_url": {"url": uri}},
            "output_format": output_format,
        }
        try:
            started = time.monotonic()
            raw = runtime.invoke_model(
                modelId=model_id, body=json.dumps(body),
                accept="application/json", contentType="application/json")
            payload = json.loads(raw["body"].read())
            page0 = (payload.get("pages") or [{}])[0]
            out["formats"][output_format] = {
                "latency_s": round(time.monotonic() - started, 3),
                "top_level_keys": sorted(payload),
                "page0_keys": sorted(page0),
                # The response adapter is deliberately tolerant about field
                # names because this was never verified. This is what
                # replaces the guessing.
                "page0_sample": json.loads(json.dumps(page0)[:2000] + "}")
                if len(json.dumps(page0)) > 2000 else page0,
            }
        except Exception as exc:  # noqa: BLE001
            out["formats"][output_format] = {"error": _err(exc)}

    # Where does the model start rejecting renders? Sets
    # COHERE_PARSE_MAX_PIXELS from evidence rather than from a guess.
    for pixels in PIXEL_LADDER:
        png = _render(pdf, pages[0], pixels)
        if png is None:
            break
        body = {
            "document": {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
            "output_format": "blocks",
        }
        try:
            runtime.invoke_model(
                modelId=model_id, body=json.dumps(body),
                accept="application/json", contentType="application/json")
            out["pixel_ladder"][pixels] = "accepted"
        except Exception as exc:  # noqa: BLE001
            out["pixel_ladder"][pixels] = _err(exc)
            break
    return out


# ---------------------------------------------------------------------------


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
        # TIMEOUT_RERANKER_S is 8s and the per-call timeout is clamped to
        # half the derived budget. If p95 is anywhere near that, the retry
        # is dead code again — the 2026-08-20 defect, in a new host.
        "reranker_budget_s": 8.0,
    }


# The sections that have to report a real observation for this run to count as
# evidence. `availability` is deliberately excluded: it can legitimately come
# back empty in a region that offers nothing, and that IS the finding.
_EVIDENCE_SECTIONS = ("chat", "chat_stream", "embed", "rerank", "parse", "latency")


def verdict(report: dict) -> dict:
    """Say plainly whether this run verified anything.

    THE POINT. Every section of this probe degrades instead of raising, so one
    run can fail completely — expired credentials, wrong region, no IAM
    permission — and still produce a well-formed JSON file. Without this the
    script printed "COMMIT THIS REPORT" and exited 0 over a report whose every
    section was a 403, which would put a file on disk that ADR-0022 treats as
    the gate on trusting the adapters while it contains no evidence at all.
    That is the same shape as the defects this migration kept turning up: not
    an error, just a thing quietly not carrying the information it claims to.
    """
    failed, skipped, missing, ok = [], [], [], []
    for name in _EVIDENCE_SECTIONS:
        # A section ABSENT from the report is not a pass. It reads as one if
        # you only test for "error"/"skipped" — the same absence-as-success
        # shape this function exists to stop — and it is how adding a section
        # to _EVIDENCE_SECTIONS without wiring it up would quietly inflate
        # the verified count.
        if name not in report:
            missing.append(name)
            continue
        section = report[name] or {}
        if "error" in section:
            failed.append(name)
        elif "skipped" in section:
            skipped.append(name)
        else:
            ok.append(name)

    auth_codes = {"UnrecognizedClientException", "InvalidClientTokenId",
                  "AccessDeniedException", "ExpiredTokenException"}
    saw_auth_failure = any(
        (report.get(n) or {}).get("error", {}).get("code") in auth_codes
        for n in _EVIDENCE_SECTIONS
    ) or (report.get("availability", {})
          .get("cohere_serverless_error", {})
          .get("code") in auth_codes)

    if saw_auth_failure and not ok:
        summary = ("could not authenticate to Bedrock; nothing was observed. "
                   "Check credentials, region and IAM permissions.")
    elif not ok:
        summary = "no section produced an observation."
    else:
        summary = (f"verified {len(ok)}/{len(_EVIDENCE_SECTIONS)} sections "
                   f"(ok={','.join(ok) or '-'}; "
                   f"failed={','.join(failed) or '-'}; "
                   f"skipped={','.join(skipped) or '-'}"
                   + (f"; MISSING={','.join(missing)}" if missing else "")
                   + ").")

    return {
        "verified_anything": bool(ok),
        "sections_ok": ok,
        "sections_failed": failed,
        "sections_skipped": skipped,
        "sections_missing": missing,
        "authentication_failed": saw_auth_failure,
        "summary": summary,
    }


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
        "embed": probe_embed(),
        "rerank": probe_rerank(),
        "parse": probe_parse(args.pdf, pages),
        "latency": probe_latency(args.latency_samples),
    }

    report["verdict"] = verdict(report)

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
