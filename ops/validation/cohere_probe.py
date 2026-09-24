"""Wire-contract probe for Cohere's own API — chat and Parse (ADR-0023).

**Neither Cohere adapter has been verified against a live call from this
codebase.** Both say so at the top. This is the gate ADR-0023 migration step
6 names: run it, read the report, correct the adapters from evidence before
either carries real traffic.

The sibling of `bedrock_probe.py`, which still covers the half that stayed on
AWS — Embed v4 and Rerank 3.5. Chat and Parse left Bedrock on 2026-09-15
because Command A+ and Parse 5 turned out to be AWS *Marketplace* SageMaker
packages rather than Bedrock models, on A100/H100 and ~$2.50/hour with no
idle state. Run both probes; neither covers the other's models.

Why this is not a formality
---------------------------
The path this ultimately replaced had three behaviours confirmed by a single
live call on 2026-07-30 that **documentation alone got wrong**: the Azure AI
Foundry catalog table said "Text only" response formats for Command A+ while
Cohere's own docs listed it under Structured Outputs, and only a real request
settled it. Those three observations are recorded as CARRIED on
`bedrock_wire.CHAT_CONVERSE` and they do not carry a second time. This is the
third host to ask them on.

What it records
---------------
  1. **Reachability.** Whether the key authenticates at all, and what the
     account can see. Cheap, and it separates "the key is wrong" from
     "the adapter is wrong" before anything else runs.
  2. **Chat, unary.** Whether `response_format: {"type": "json_object"}` is
     honoured — the single most important field in either adapter, because
     every typed-output guard in `orchestrator_validators.py` assumes the
     model was actually asked for JSON (hard rule 4). Plus where reasoning
     lands, whether Cohere's `<|START_TEXT|>`/`<|END_TEXT|>` sentinels
     survive, and the real shape of `message.content` and `usage`.
  3. **Chat, streaming.** The SSE event vocabulary: which `type` carries
     text, how the delta nests, and where usage lands. The query path is SSE
     end to end and Laravel re-broadcasts each frame on Reverb, so a missed
     delta is a silently truncated answer rather than an error.
  4. **The system-message divergence**, asked directly. Cohere v2 takes
     system as a MESSAGE; Bedrock Converse takes it as a top-level
     parameter. Reversing it fails silently — the model still answers
     fluently, just without the grounding rules — so the probe sends a
     system prompt with a checkable instruction in it and records whether
     the answer obeyed.
  5. **Parse**, in both output formats, with the full key shape of
     `pages[0]`. This contract has **never** been verified on any of the
     three hosts it has run on.
  6. **The pixel ladder.** Where Parse starts rejecting renders, which is
     what turns `COHERE_PARSE_MAX_PIXELS` from a guess into a measurement.
  7. Error shapes and latency, so the retry ladder in `_invoke` is tuned
     against something real rather than against Bedrock's behaviour.

On secrets
----------
Unlike the Bedrock probe there IS a credential here, and the report is meant
to be committed. `COHERE_API_KEY` is never written to the report, never
logged, and never echoed in an error — `_err` truncates provider messages and
`_redact` strips anything key-shaped that a provider might mirror back. The
report records only that a key was present and how long it was.

Usage:
    COHERE_API_KEY=... \\
    uv run python ops/validation/cohere_probe.py \\
        --pdf src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf \\
        --pages 1,7 --out ops/validation/reports/
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fastapi"))

from _probe_verdict import compute_verdict  # noqa: E402

# The wire contract lives with the adapters it describes, because it is a
# claim ABOUT them and tests/test_bedrock_wire_contract.py holds the two
# together. Importing it here turns this probe's output from a transcript
# into a diff.
#
# Guarded, and degrading to a skip rather than an exception, for the same
# reason every section below degrades: this script has to stay runnable from
# an operator's laptop against a checkout that may not have the FastAPI
# package importable. Losing the diff must not cost the observations.
try:
    from app.services.cohere_wire import diff_report as _diff_report
except Exception as _exc:  # noqa: BLE001 — any import failure, not just ImportError
    _DIFF_IMPORT_ERROR: str | None = f"{type(_exc).__name__}: {_exc}"

    def _diff_report(report: dict) -> dict:  # type: ignore[misc]
        return {
            "skipped": f"app.services.cohere_wire unavailable ({_DIFF_IMPORT_ERROR})"
        }
else:
    _DIFF_IMPORT_ERROR = None


#: Same ladder the Bedrock probe used. The 4 MP default renders US Letter at
#: ~210 DPI and an A0 plan sheet at ~55 DPI; the point of the ladder is to
#: find the vendor's real ceiling instead of guessing under it.
PIXEL_LADDER = (1_900_000, 4_000_000, 8_000_000, 12_000_000, 20_000_000)

#: Sentinel tokens Cohere wraps JSON-mode output in. Confirmed on Foundry
#: 2026-07-30; whether they survive on Cohere's own API is what step 2 asks.
SENTINELS = ("<|START_TEXT|>", "<|END_TEXT|>")

#: A system prompt whose obedience is checkable from the answer alone. If the
#: adapter ever sends system as a user turn — the silent failure this probe
#: exists to catch — the model answers the question and ignores this.
_SYSTEM_CANARY = (
    "You are a test harness. Whatever you are asked, your entire reply must "
    "be exactly the word GROUNDED and nothing else."
)
_JSON_PROMPT = (
    'Reply with JSON only: {"ok": true, "unit": "ppm"}. No prose, no code fence.'
)

_KEY_SHAPED = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def _redact(text: str) -> str:
    """Strip anything key-shaped before it reaches a committed report.

    Providers mirror request material back in error messages more often than
    they should. This report is meant to be committed to a repository that
    has already leaked one live credential (`scripts/phase0_acceptance.sh`,
    still in git history), so the cost of a false positive here — a redacted
    model id — is far below the cost of a false negative.
    """
    return _KEY_SHAPED.sub("<redacted>", text)


def _api_key() -> str:
    return (os.environ.get("COHERE_API_KEY") or "").strip()


def _base_url() -> str:
    return (os.environ.get("COHERE_BASE_URL") or "https://api.cohere.com").rstrip("/")


def _chat_model() -> str:
    return (
        os.environ.get("COHERE_CHAT_MODEL") or ""
    ).strip() or "command-a-plus-05-2026"


def _parse_model() -> str:
    return (os.environ.get("COHERE_PARSE_MODEL") or "").strip() or "parse-v5.0"


def _client(timeout: float = 180.0):
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=15.0),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


def _err(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": _redact(str(exc))[:400]}


def _http_err(response: Any) -> dict[str, Any]:
    body = ""
    try:
        body = response.text[:400]
    except Exception:  # noqa: BLE001
        pass
    return {
        "type": "HTTPStatusError",
        "status": response.status_code,
        "code": _classify(response.status_code),
        "message": _redact(body),
    }


def _classify(status: int) -> str:
    """A name for the status, so the verdict can reason about it.

    Deliberately coarse. The adapter's own `_REJECTED_STATUS` /
    `_RETRYABLE_STATUS` split is the one that matters operationally; this is
    only here so "the key is wrong" reads differently from "the model name is
    wrong" in a report someone opens six months from now.
    """
    if status in (401, 403):
        return "AuthenticationError"
    if status == 404:
        return "NotFound"
    if status == 429:
        return "RateLimited"
    if 400 <= status < 500:
        return "ClientError"
    return "ServerError"


# ---------------------------------------------------------------------------
# 1. Reachability — is it the key, or is it us?
# ---------------------------------------------------------------------------


def probe_reachability() -> dict[str, Any]:
    key = _api_key()
    if not key:
        return {"skipped": "COHERE_API_KEY unset"}

    out: dict[str, Any] = {
        "base_url": _base_url(),
        # Never the value. Length alone distinguishes "unset" from
        # "truncated by a copy-paste" without putting a secret on disk.
        "key_length": len(key),
        "chat_model": _chat_model(),
        "parse_model": _parse_model(),
    }

    # Best-effort. A 404 here is NOT a failure and is recorded as an
    # observation: it tells the next reader this route does not exist on
    # this host, which is worth more than an empty section.
    try:
        with _client(timeout=30.0) as client:
            response = client.get(f"{_base_url()}/v1/models")
        out["models_endpoint"] = {"status": response.status_code}
        if response.status_code < 300:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = {}
            names = [
                m.get("name")
                for m in (payload.get("models") or [])
                if isinstance(m, dict)
            ]
            out["models_endpoint"]["count"] = len(names)
            out["models_visible"] = sorted(n for n in names if n)
            # The two the deployment actually needs. A key that authenticates
            # but is not entitled to Parse deploys cleanly and then sends
            # every scanned page to tesseract — see aws-preflight A-08.
            out["chat_model_listed"] = _chat_model() in (out["models_visible"] or [])
            out["parse_model_listed"] = _parse_model() in (out["models_visible"] or [])
        elif response.status_code in (401, 403):
            out["error"] = _http_err(response)
    except Exception as exc:  # noqa: BLE001
        out["models_endpoint"] = {"error": _err(exc)}
    return out


# ---------------------------------------------------------------------------
# 2. Chat, unary — the three Foundry questions, asked on a third host
# ---------------------------------------------------------------------------


def _parses(text: str) -> bool:
    stripped = text
    for sentinel in SENTINELS:
        stripped = stripped.replace(sentinel, "")
    try:
        json.loads(stripped.strip())
    except Exception:  # noqa: BLE001
        return False
    return True


def _read_message(payload: Any) -> dict[str, Any]:
    """Everything the adapter's `_extract_content` and `_extract_usage` read.

    Recorded as SHAPE rather than content: which keys arrived, in which
    nesting. That is what `cohere_wire.CHAT_V2`'s evidence paths diff
    against, and it is also what tells you which of the tolerated spellings
    the adapter can stop carrying.
    """
    if not isinstance(payload, dict):
        return {"top_level_type": type(payload).__name__}

    message = payload.get("message")
    blocks = []
    content_kind = None
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            content_kind = "list"
            blocks = [b for b in content if isinstance(b, dict)]
        elif isinstance(content, str):
            content_kind = "str"

    text = ""
    if content_kind == "list":
        text = "".join(b["text"] for b in blocks if isinstance(b.get("text"), str))
    elif content_kind == "str":
        text = message.get("content") or ""  # type: ignore[union-attr]
    elif isinstance(payload.get("text"), str):
        text = payload["text"]

    usage = payload.get("usage")
    return {
        "top_level_keys": sorted(payload),
        "content_kind": content_kind,
        # (2) Where does reasoning live? Foundry used a sibling field;
        #     Converse uses a content block. Both are looked for.
        "content_block_keys": sorted({k for b in blocks for k in b}),
        "message_sibling_keys": sorted(k for k in (message or {}) if k != "content")
        if isinstance(message, dict)
        else [],
        "usage_keys": sorted(usage) if isinstance(usage, dict) else None,
        "usage_tokens_keys": sorted(usage["tokens"])
        if isinstance(usage, dict) and isinstance(usage.get("tokens"), dict)
        else None,
        # (3) Do the sentinels survive this host's runtime?
        "sentinels_present": [s for s in SENTINELS if s in text],
        "text_head": _redact(text[:400]),
        "parses_as_json": _parses(text),
        "adapter_extract_ok": _adapter_extract_ok(payload),
    }


def _adapter_extract_ok(payload: Any) -> Any:
    """Run the REAL `_extract_content` against the real body.

    The key-shape diff answers "does the response match the declaration".
    This answers the question an operator actually has: would the shipped
    adapter have read this? They can differ — the adapter is deliberately
    tolerant across spellings the contract marks TOLERATED — and when they
    do, this is the one that decides whether anything is broken.
    """
    extract, unavailable = _load_adapter("_extract_content")
    if extract is None:
        return {"skipped": unavailable}
    try:
        return {"ok": True, "chars": len(extract(payload))}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": type(exc).__name__,
            "message": _redact(str(exc))[:200],
        }


def probe_chat() -> dict[str, Any]:
    if not _api_key():
        return {"skipped": "COHERE_API_KEY unset"}

    url = f"{_base_url()}/v2/chat"
    model = _chat_model()
    out: dict[str, Any] = {"model": model}

    variants: tuple[tuple[str, dict[str, Any]], ...] = (
        # (1) The most important question in this file. Hard rule 4 assumes
        #     the model was actually asked for JSON.
        ("without_response_format", {}),
        ("with_response_format", {"response_format": {"type": "json_object"}}),
    )

    with _client() as client:
        for label, extra in variants:
            body = {
                "model": model,
                "messages": [{"role": "user", "content": _JSON_PROMPT}],
                "temperature": 0.0,
                "max_tokens": 256,
                "stream": False,
                **extra,
            }
            try:
                started = time.monotonic()
                response = client.post(url, content=json.dumps(body).encode())
                elapsed = time.monotonic() - started
            except Exception as exc:  # noqa: BLE001
                out[label] = {"error": _err(exc)}
                continue
            if response.status_code >= 300:
                out[label] = {"error": _http_err(response)}
                continue
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                out[label] = {
                    "error": {
                        "type": "NonJsonResponse",
                        "message": _err(exc)["message"],
                    }
                }
                continue
            out[label] = {"latency_s": round(elapsed, 3), **_read_message(payload)}

        # (4) The system-message divergence, asked directly rather than
        #     inferred. If system arrives as a user turn the model answers
        #     the question and ignores the canary, and NOTHING else in this
        #     report would show it.
        out["system_is_a_message"] = _probe_system_placement(client, url, model)

    return out


def _probe_system_placement(client: Any, url: str, model: str) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_CANARY},
            {"role": "user", "content": "What is the capital of France?"},
        ],
        "temperature": 0.0,
        # Reasoning is on by default and spends from this budget first. At
        # 32 the 2026-09-23 run got an all-thinking reply with no answer and
        # reported that as "system prompt ignored".
        "max_tokens": 1024,
        "stream": False,
    }
    try:
        response = client.post(url, content=json.dumps(body).encode())
    except Exception as exc:  # noqa: BLE001
        return {"error": _err(exc)}
    if response.status_code >= 300:
        return {"error": _http_err(response)}
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": {"type": "NonJsonResponse", "message": _err(exc)["message"]}}

    shape = _read_message(payload)
    answer = (shape.get("text_head") or "").strip()
    if not answer:
        # No answer text says nothing about where the system prompt went.
        # Reporting False here accused a correct request of the silent
        # failure below, over a reply that was all reasoning.
        return {
            "role_system_accepted": True,
            "answer_head": "",
            "obeyed_the_system_prompt": None,
            "note": "inconclusive: the reply carried no answer text (content "
            f"block keys {shape.get('content_block_keys')}), so it cannot show "
            "whether role:'system' was applied.",
        }
    obeyed = answer.upper().startswith("GROUNDED")
    return {
        "role_system_accepted": True,
        "answer_head": answer[:120],
        "obeyed_the_system_prompt": obeyed,
        "note": (
            "obeyed=True means role:'system' reached the model as a system "
            "prompt. obeyed=False with a 200 is the SILENT failure this "
            "probe exists for: the call succeeded, the answer is fluent, and "
            "the grounding rules were never applied — while the citation "
            "guards go on enforcing against the output."
            if not obeyed
            else "role:'system' is honoured; llm_cohere._build_request is right."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Chat, streaming — the SSE event vocabulary
# ---------------------------------------------------------------------------


def probe_chat_stream() -> dict[str, Any]:
    if not _api_key():
        return {"skipped": "COHERE_API_KEY unset"}

    body = {
        "model": _chat_model(),
        "messages": [{"role": "user", "content": "Count from one to five in words."}],
        "temperature": 0.0,
        # Reasoning is on by default and spends from this budget before any
        # answer text; 128 could end a stream with nothing but thinking.
        "max_tokens": 1024,
        "stream": True,
    }
    out: dict[str, Any] = {"model": _chat_model()}
    event_types: dict[str, int] = {}
    delta_shapes: set[str] = set()
    line_kinds: dict[str, int] = {}
    first_text_at: float | None = None
    pieces: list[str] = []
    usage_event: dict[str, Any] | None = None
    content_type = ""
    unparsed: list[str] = []
    _, adapter_unavailable = _load_adapter("_delta_text")

    try:
        started = time.monotonic()
        with (
            _client() as client,
            client.stream(
                "POST",
                f"{_base_url()}/v2/chat",
                content=json.dumps(body).encode(),
                # What llm_cohere._headers(stream=True) sends. The first live
                # run sent application/json here and parsed no event at all.
                headers={"Accept": "text/event-stream"},
            ) as response,
        ):
            if response.status_code >= 300:
                response.read()
                return {"error": _http_err(response)}
            content_type = response.headers.get("content-type", "")
            for line in response.iter_lines():
                kind_of_line = _line_kind(line)
                line_kinds[kind_of_line] = line_kinds.get(kind_of_line, 0) + 1
                event = _stream_event(line)
                if event is None:
                    if line.strip() and len(unparsed) < 10_000:
                        unparsed.append(line)
                    continue
                kind = str(event.get("type") or "<untyped>")
                event_types[kind] = event_types.get(kind, 0) + 1
                delta_shapes.add(_delta_shape(event))
                piece = _stream_text(event)
                if piece:
                    if first_text_at is None:
                        first_text_at = time.monotonic() - started
                    pieces.append(piece)
                if "usage" in event or (
                    isinstance(event.get("delta"), dict) and "usage" in event["delta"]
                ):
                    usage_event = {"type": kind, "keys": sorted(event)}
            if not event_types and unparsed:
                # The adapter's last resort: the whole body as one reply.
                whole_reply, _ = _load_adapter("_whole_body_reply")
                extract, _ = _load_adapter("_extract_content")
                whole = whole_reply("\n".join(unparsed)) if whole_reply else None
                if whole is not None and extract is not None:
                    event_types["<whole-reply>"] = 1
                    piece = extract(whole)
                    if piece:
                        first_text_at = time.monotonic() - started
                        pieces.append(piece)
        elapsed = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return {"error": _err(exc)}

    text = "".join(pieces)
    out.update(
        {
            "total_s": round(elapsed, 3),
            "content_type": content_type,
            # Framing, by line: `data`, `event`, `json` (a bare JSON object),
            # `blank`, `other`. Never the lines themselves.
            "line_kinds": dict(sorted(line_kinds.items())),
            "first_text_s": round(first_text_at, 3)
            if first_text_at is not None
            else None,
            "event_types": dict(sorted(event_types.items())),
            # Which of _delta_text's tolerated spellings actually arrives.
            # One entry here means two of the three branches are dead code
            # and can be deleted once this report is committed.
            "delta_shapes_seen": sorted(s for s in delta_shapes if s != "none"),
            "usage_event": usage_event,
            "text_chars": len(text),
            "text_head": _redact(text[:200]),
            # The thing that matters operationally: did the shipped
            # `_delta_text` read any of it? A stream the adapter cannot read
            # raises CohereResponseShapeError rather than answering blank.
            #
            # None, not False, when the adapter could not be imported: the
            # probe has no opinion in that case and must not express one.
            "adapter_read_any_delta": None if adapter_unavailable else bool(pieces),
            "adapter_unavailable": adapter_unavailable,
        }
    )
    if not event_types:
        # A 200 that yielded no event observed nothing about the stream. The
        # 2026-09-23 run reported this section "ok" with `event_types: {}` --
        # verified, over a stream nothing could read. Say it failed, with
        # the framing evidence beside it.
        out["error"] = {
            "type": "NoStreamEvents",
            "message": (
                f"HTTP {response.status_code}, content-type {content_type or '(none)'!r}, "
                f"line kinds {out['line_kinds']}: no line parsed as an event."
            ),
        }
    return out


def _line_kind(line: str) -> str:
    if not line.strip():
        return "blank"
    if line.startswith("data:"):
        return "data"
    if line.lstrip().startswith("{"):
        return "json"
    field, sep, _ = line.partition(":")
    if sep and field in {"event", "id", "retry"}:
        return field
    if line.startswith(":"):
        return "comment"
    return "other"


def _stream_event(line: str) -> dict[str, Any] | None:
    """Delegate framing to the REAL adapter, like `_stream_text` does for
    text. A probe with its own line parser measured its own parser: the
    2026-09-23 run parsed nothing, and that said nothing about whether the
    adapter would have. Falls back to `data:` lines only when the adapter
    cannot be imported, which the report already flags."""
    parse_line, _ = _load_adapter("_sse_events")
    if parse_line is not None:
        return parse_line(line)
    if not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _delta_shape(event: dict[str, Any]) -> str:
    """Name the nesting this event carries text in, if any."""
    delta = event.get("delta")
    if isinstance(delta, dict):
        message = delta.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return "delta.message.content.text"
            if isinstance(content, str):
                return "delta.message.content"
        if isinstance(delta.get("text"), str):
            return "delta.text"
    if isinstance(event.get("text"), str):
        return "text"
    return "none"


def _load_adapter(name: str) -> tuple[Any, str | None]:
    """Import one helper out of `llm_cohere`, or say why not.

    Returning the reason matters more than it looks. A first draft swallowed
    the ImportError and returned None, so a run made from a shell without
    FASTAPI_SERVICE_KEY set — `app.config` instantiates Settings at import —
    printed "_delta_text read NOTHING <-- adapter is wrong" over a stream it
    had just parsed correctly. That is a probe accusing the code it exists
    to check, which is worse than saying nothing: the operator goes and
    edits a working adapter.

    Found by running this script against a fake server before trusting it.
    """
    try:
        module = __import__("app.agent.llm_cohere", fromlist=[name])
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    return getattr(module, name), None


def _stream_text(event: dict[str, Any]) -> str | None:
    """Delegate to the REAL adapter, so this measures the shipped code.

    Reimplementing the extraction here would make the probe agree with
    itself rather than with `llm_cohere`, which is the opposite of what it
    is for.
    """
    delta_text, _ = _load_adapter("_delta_text")
    if delta_text is None:
        return None
    return delta_text(event)


# ---------------------------------------------------------------------------
# 4. Parse — never verified on any of three hosts
# ---------------------------------------------------------------------------


def _render_sized(pdf: Path, page: int, max_pixels: int) -> tuple[bytes, int] | None:
    """Render ``page`` to fill ``max_pixels``; return (png, actual pixels).

    No scale ceiling. The first live run (2026-09-24) capped the scale at
    4.0, which on a Letter page is ~7.8 MP: its 8, 12 and 20 MP rungs sent
    byte-identical images, and the report read as "accepted up to 20 MP"
    when nothing above ~7.8 MP had been sent. The actual pixel count now
    travels with each rung so the report cannot overstate what it tested.
    """
    try:
        import pypdfium2
    except ImportError:
        return None
    document = pypdfium2.PdfDocument(str(pdf))
    try:
        target = document[page - 1]
        width, height = target.get_size()
        scale = (max_pixels / (width * height)) ** 0.5
        image = target.render(scale=scale).to_pil()
        if image.width * image.height > max_pixels:
            shrink = (max_pixels / (image.width * image.height)) ** 0.5
            image = image.resize(
                (max(1, int(image.width * shrink)), max(1, int(image.height * shrink)))
            )
        import io as _io

        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), image.width * image.height
    finally:
        document.close()


def _render(pdf: Path, page: int, max_pixels: int) -> bytes | None:
    rendered = _render_sized(pdf, page, max_pixels)
    return None if rendered is None else rendered[0]


def probe_parse(pdf: Path | None, pages: list[int]) -> dict[str, Any]:
    if not _api_key():
        return {"skipped": "COHERE_API_KEY unset"}
    if pdf is None or not pdf.exists():
        return {"skipped": "no --pdf given"}

    url = f"{_base_url()}/v2/parse"
    model = _parse_model()
    out: dict[str, Any] = {"model": model, "formats": {}, "pixel_ladder": {}}

    with _client() as client:
        for output_format in ("blocks", "markdown"):
            png = _render(pdf, pages[0], 4_000_000)
            if png is None:
                out["formats"][output_format] = {"skipped": "pypdfium2 unavailable"}
                continue
            body = _parse_body(model, png, output_format)
            try:
                started = time.monotonic()
                response = client.post(url, content=json.dumps(body).encode())
                elapsed = time.monotonic() - started
            except Exception as exc:  # noqa: BLE001
                out["formats"][output_format] = {"error": _err(exc)}
                continue
            if response.status_code >= 300:
                out["formats"][output_format] = {"error": _http_err(response)}
                continue
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                out["formats"][output_format] = {
                    "error": {
                        "type": "NonJsonResponse",
                        "message": _err(exc)["message"],
                    }
                }
                continue
            out["formats"][output_format] = {
                "latency_s": round(elapsed, 3),
                **_read_parse(payload),
            }

        # Where does the model start rejecting renders? This is what sets
        # COHERE_PARSE_MAX_PIXELS from evidence. A too-high value is loud —
        # the request 4xxs and the page falls back to tesseract — so the
        # ladder climbs until something refuses rather than stopping early.
        for pixels in PIXEL_LADDER:
            rendered = _render_sized(pdf, pages[0], pixels)
            if rendered is None:
                break
            png, actual_pixels = rendered
            body = _parse_body(model, png, "blocks")
            try:
                response = client.post(url, content=json.dumps(body).encode())
                if response.status_code >= 300 and response.status_code not in _SIZE_REJECTION_STATUS:
                    # Refused for a reason that says nothing about render size
                    # -- a bad key, a wrong model name, a rate limit, a server
                    # fault. Recording it as a rung (status + accepted=False,
                    # no `error`) made it read as an observation: a run whose
                    # every Parse call was a 401 reported "ok=parse" and
                    # "verified", found by running this probe through
                    # ops/rehearsal/run_cohere_probe.sh against a fake that
                    # 401s everything.
                    out["pixel_ladder"][str(pixels)] = {"error": _http_err(response)}
                    break
                out["pixel_ladder"][str(pixels)] = {
                    "png_bytes": len(png),
                    "pixels": actual_pixels,
                    "status": response.status_code,
                    "accepted": response.status_code < 300,
                }
                if response.status_code >= 300:
                    out["pixel_ladder"][str(pixels)]["code"] = _classify(
                        response.status_code
                    )
                    break
            except Exception as exc:  # noqa: BLE001
                out["pixel_ladder"][str(pixels)] = {"error": _err(exc)}
                break

    return out


# Statuses by which Parse can refuse a render for being too large. Only these
# make a rejected ladder rung an observation of the pixel limit; any other
# refusal is an ordinary failed call.
_SIZE_REJECTION_STATUS = frozenset({400, 413, 422})


def _parse_body(model: str, png: bytes, output_format: str) -> dict[str, Any]:
    """Exactly what `cohere_parse_client` puts on the wire.

    Built here rather than imported because the adapter assembles it across
    `_request_body` and `_invoke`, and importing half of that would probe a
    shape nothing sends.
    """
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return {
        "model": model,
        # A bare string. The object form was refused with a 400 on the
        # first live run (2026-09-23), and every ladder rung with it.
        "document": {"type": "image_url", "image_url": uri},
        "output_format": output_format,
    }


def _read_parse(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"top_level_type": type(payload).__name__}
    pages = payload.get("pages")
    page0 = pages[0] if isinstance(pages, list) and pages else {}
    if not isinstance(page0, dict):
        page0 = {}
    blocks = page0.get("blocks")
    block_list = (
        [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
    )
    return {
        "top_level_keys": sorted(payload),
        "page0_keys": sorted(page0),
        "block_count": len(block_list),
        # Which of the adapter's three tolerated text spellings arrive, and
        # which table/image spellings. Collapsing these is what a committed
        # report buys — delete the losers rather than carrying all of them.
        "block_keys": sorted({k for b in block_list for k in b}),
        # The Cohere SDK nests a block's fields under a key named by its
        # type ({"type": "text", "text": {"content": ...}}). Without these,
        # the diff can see that `text` arrived but not whether it held the
        # text or an object holding it -- which is the difference between a
        # page read correctly and one whose text is a dict's repr.
        "block_payload_keys": sorted(
            {
                k
                for b in block_list
                if isinstance(b.get(str(b.get("type"))), dict)
                for k in b[str(b.get("type"))]
            }
        ),
        "block_types": sorted(
            {str(b.get("type")) for b in block_list if b.get("type")}
        ),
        "adapter_page_ok": _adapter_page_ok(payload),
    }


def _adapter_page_ok(payload: Any) -> Any:
    """Run the REAL `_page_from_payload`. The question an operator has.

    Until 2026-09-15 an unrecognised body here produced a silently blank
    page with no fallback and no metric. It now fails loudly, and this is
    where a probe run finds out which it would have been.
    """
    try:
        from app.services.ingest.cohere_parse_client import _page_from_payload
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"{type(exc).__name__}: {exc}"}
    try:
        result = _page_from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}
    return {
        "ok": bool(result.request_succeeded),
        "chars": len(result.text),
        "tables": len(getattr(result, "tables", ()) or ()),
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# 5. Latency
# ---------------------------------------------------------------------------


def probe_latency(samples: int) -> dict[str, Any]:
    if not _api_key():
        return {"skipped": "COHERE_API_KEY unset"}
    if samples <= 0:
        return {"skipped": "--latency-samples 0"}

    body = {
        "model": _chat_model(),
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "temperature": 0.0,
        "max_tokens": 8,
        "stream": False,
    }
    timings: list[float] = []
    errors: list[dict[str, Any]] = []
    with _client(timeout=60.0) as client:
        for _ in range(samples):
            try:
                started = time.monotonic()
                response = client.post(
                    f"{_base_url()}/v2/chat", content=json.dumps(body).encode()
                )
                if response.status_code >= 300:
                    errors.append(_http_err(response))
                    continue
                timings.append(time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001
                errors.append(_err(exc))

    if not timings:
        return {"error": errors[0] if errors else {"type": "NoSamples"}}
    return {
        "samples": len(timings),
        "min_s": round(min(timings), 3),
        "median_s": round(statistics.median(timings), 3),
        "max_s": round(max(timings), 3),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

_EVIDENCE_SECTIONS = ("chat", "chat_stream", "parse", "latency")


def _is_auth_failure(section: dict) -> bool:
    return (section.get("error") or {}).get("code") == "AuthenticationError"


def verdict(report: dict) -> dict:
    """Say plainly whether this run verified anything.

    The reasoning lives in ops/validation/_probe_verdict.py, which both
    probes share — see that module's docstring for why a report that
    verifies nothing must not read as a pass, and what shipped once because
    it did. This function is the Cohere half: which sections count as
    evidence, and how a credentials failure is recognised here (an HTTP
    401/403 rather than a botocore error code).

    ``reachability`` is checked for an auth failure too even though it is
    not an evidence section, because it is the first call a run makes — so
    it is where a wrong key shows up before anything else has had a chance
    to.
    """
    result = compute_verdict(
        report,
        sections=_EVIDENCE_SECTIONS,
        is_auth_failure=_is_auth_failure,
        auth_hint="Check COHERE_API_KEY and that its plan covers both models.",
    )
    if not result["authentication_failed"] and _is_auth_failure(
        report.get("reachability") or {}
    ):
        result["authentication_failed"] = True
        if not result["verified_anything"]:
            result["summary"] = (
                "could not authenticate to Cohere; nothing was observed. "
                "Check COHERE_API_KEY and that its plan covers both models."
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=None)
    parser.add_argument("--pages", default="1")
    parser.add_argument("--latency-samples", type=int, default=5)
    parser.add_argument("--out", type=Path, default=Path("ops/validation/reports"))
    args = parser.parse_args()

    pages = [int(p) for p in args.pages.split(",") if p.strip()]

    report: dict[str, Any] = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "base_url": _base_url(),
        "reachability": probe_reachability(),
        "chat": probe_chat(),
        "chat_stream": probe_chat_stream(),
        "parse": probe_parse(args.pdf, pages),
        "latency": probe_latency(args.latency_samples),
    }

    report["verdict"] = verdict(report)
    # Field-by-field against app/services/cohere_wire.py. The verdict above
    # answers "did this run observe anything"; this answers "does what it
    # observed match what the adapters believe", which is a different
    # question and the one that names the file to edit.
    report["contract_diff"] = _diff_report(report)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = args.out / f"cohere_probe_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(json.dumps(report, indent=2, default=str))
    print(f"\nreport written to {path}", file=sys.stderr)

    v = report["verdict"]
    if not v["verified_anything"]:
        print(
            f"\nPROBE FAILED — {v['summary']}\n"
            "DO NOT commit this report as evidence: it verifies nothing, and a\n"
            "report on file is exactly what ADR-0023 treats as the gate on\n"
            "trusting the adapters. Fix the access problem and re-run.",
            file=sys.stderr,
        )
        return 1

    if v["sections_failed"] or v["sections_skipped"]:
        print(f"\nPARTIAL — {v['summary']}", file=sys.stderr)

    # The three Foundry questions, answered or not, called out by name —
    # they are the reason this script exists and they are easy to lose in a
    # 400-line JSON dump.
    _report_headlines(report)

    diff = report["contract_diff"]
    broken = diff.get("required_fields_missing") or {}
    undeclared = diff.get("undeclared_fields") or {}

    if undeclared:
        print(
            "\nUNDECLARED FIELDS observed — nothing reads these, and "
            "app/services/cohere_wire.py does not declare them:",
            file=sys.stderr,
        )
        for call, keys in sorted(undeclared.items()):
            print(f"  {call}: {', '.join(keys)}", file=sys.stderr)

    if broken:
        print(
            "\nCONTRACT VIOLATED — the probe observed these calls and a "
            "REQUIRED field was absent from each:",
            file=sys.stderr,
        )
        for call, keys in sorted(broken.items()):
            print(f"  {call}: missing {', '.join(keys)}", file=sys.stderr)
        print(
            "\nEach line names an adapter that reads a field Cohere did not "
            "send. Correct the adapter AND app/services/cohere_wire.py "
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
        "\nCOMMIT THIS REPORT. Both Cohere adapters say [UNVERIFIED] at the "
        "top until one exists, and ADR-0023 migration step 6 is this run.",
        file=sys.stderr,
    )
    return 0


def _report_headlines(report: dict) -> None:
    """Print the handful of answers the whole run is for."""
    chat = report.get("chat") or {}
    with_json = chat.get("with_response_format") or {}
    system = chat.get("system_is_a_message") or {}
    stream = report.get("chat_stream") or {}
    parse_formats = (report.get("parse") or {}).get("formats") or {}

    lines: list[str] = []

    if "parses_as_json" in with_json:
        lines.append(
            f"  JSON mode honoured:        {with_json['parses_as_json']}"
            + (
                ""
                if with_json["parses_as_json"]
                else "   <-- hard rule 4 depends on this"
            )
        )
    if "sentinels_present" in with_json:
        seen = with_json["sentinels_present"]
        lines.append(
            f"  Cohere sentinels present:  {bool(seen)}"
            + (
                ""
                if seen
                else "   (clean_model_text's stripping is a no-op on this host)"
            )
        )
    if "obeyed_the_system_prompt" in system:
        obeyed = system["obeyed_the_system_prompt"]
        lines.append(
            f"  role:'system' honoured:    {obeyed}"
            + (
                "   (inconclusive: no answer text)"
                if obeyed is None
                else ""
                if obeyed
                else "   <-- SILENT failure: grounding rules never applied"
            )
        )
    if "delta_shapes_seen" in stream:
        lines.append(
            f"  streaming delta shape:     {stream['delta_shapes_seen'] or 'NONE READ'}"
        )
    if stream.get("adapter_unavailable"):
        # NOT an accusation. The probe could not import the adapter, so it
        # has no opinion on whether the adapter works.
        lines.append(
            f"  streaming:                 adapter not importable "
            f"({stream['adapter_unavailable']})"
        )
        lines.append(
            "                             -- run it via ops/validation/cohere_probe.sh"
        )
    elif stream.get("adapter_read_any_delta") is False:
        lines.append(
            "  streaming:                 _delta_text read NOTHING   <-- adapter is wrong"
        )
    for fmt, section in sorted(parse_formats.items()):
        ok = (section.get("adapter_page_ok") or {}).get("ok")
        if ok is not None:
            lines.append(f"  parse[{fmt}] adapter reads:  {ok}")

    if lines:
        print(
            "\nHEADLINES — the questions this probe exists to answer:", file=sys.stderr
        )
        for line in lines:
            print(line, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
