#!/usr/bin/env python3
"""Stand-in Azure AI Foundry backend for the CI money-path smoke test.

The real money path (upload -> parse -> embed -> query -> cited answer)
talks to three network services GeoRAG normally gets from Azure AI Foundry
or a self-hosted sidecar:

  1. POST /providers/cohere/v2/embed   -- Cohere Embed v4 (dense vectors)
  2. POST /v1/chat/completions          -- the LLM (OpenAI-compatible, used
                                            when LLM_BACKEND=vllm)

None of those are available in a GitHub-hosted CI runner without real
credentials (see the eval-gate.yml workflow's comment block for the same
constraint), so this script fakes just enough of the wire contract for the
smoke test's ingest -> embed -> query loop to complete and produce a
GeoRAGResponse with a real citation.

Embedding fidelity note
------------------------
The /embed endpoint below is NOT a real semantic embedding model. It is a
deterministic "marker + hashing-trick" encoder: a short list of phrases we
know appear in the fixture PDF (see _MARKER_PHRASES) each get a dedicated,
heavily-weighted vector dimension, plus a generic bag-of-words hashing
component for uniqueness. Any text containing a marker phrase (both the
fixture's own passages AND the smoke test's query, which deliberately asks
about one of those markers) ends up with high cosine similarity on that
dimension, comfortably clearing app/config.py's RETRIEVAL_QUALITY_THRESHOLD
(0.5) regardless of everything else in the text. This is a controlled rig,
not a claim that retrieval quality is being tested here -- only that the
plumbing (Qdrant round-trip, payload contract, score gate, citation
assembly) is exercised end-to-end. Real retrieval-quality testing is the
job of scripts/run_golden_benchmark.py (see eval-gate.yml).

Sparse encoding is left alone (the FastAPI process loads the real SPLADE++
model locally -- SPARSE_SERVICE_URL is NOT pointed at this stub) since it's
a small (~440 MB), fast-loading, purely local model with no external network
dependency, unlike the multi-GB self-hosted dense embedder this stub exists
to avoid.

Chat completions
-----------------
The single required call on the smoke test's happy path is the final
answer-synthesis call (app/agent/agentic_retrieval/nodes.py's assemble_node
-> app/agent/llm_calls._call_llm), which sends plain prose back to the
client -- GeoRAGResponse.citations is built from the deterministic Qdrant
retrieval results, not by parsing structured JSON out of the LLM's answer,
so this stub only needs to return *some* non-empty text. Handles both the
streaming (SSE) and non-streaming request shapes since queries.py always
sets stream=true, but implementing both keeps this fixture reusable.
The low-confidence keyword-classifier LLM fallback call (a one-word intent
label) is a separate call shape this stub does NOT special-case -- the
smoke test's query phrasing is chosen so the keyword classifier resolves
with high confidence and that fallback path never fires (see
e2e_smoke_query.py's docstring).

Usage::

    python tests/e2e_smoke/stub_backend.py --port 8899
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 1024

# Phrases known to appear in tests/fixtures/ocr/PLS-2024-Technical-Report.pdf
# (a ReportLab-generated NI 43-101 style fixture about Fission Uranium
# Corp's Patterson Lake South property). Each gets its own dominant vector
# dimension -- see module docstring.
_MARKER_PHRASES = [
    "pls-22-08",
    "uranium",
    "patterson lake south",
    "fission uranium",
    "u3o8",
]


def _embed_one(text: str) -> list[float]:
    t = (text or "").lower()
    vec = [0.0] * DIM

    for i, marker in enumerate(_MARKER_PHRASES):
        if marker in t:
            vec[i] = 8.0

    # Generic hashing-trick background component so distinct texts still
    # get distinct (if not semantically meaningful) vectors.
    words = re.findall(r"[a-z0-9]+", t)
    free_dims = DIM - len(_MARKER_PHRASES)
    for w in words:
        h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)  # noqa: S324 — non-crypto use
        idx = len(_MARKER_PHRASES) + (h % free_dims)
        sign = 1.0 if (h // free_dims) % 2 == 0 else -1.0
        vec[idx] += sign * 0.3

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


_SSE_ANSWER = (
    "Based on the retrieved passages, hole PLS-22-08 returned high-grade "
    "uranium mineralisation at the Patterson Lake South property. "
    "[DATA-1]"
)


class StubHandler(BaseHTTPRequestHandler):
    server_version = "GeoRAGStubFoundry/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 — stdlib override
        # Quiet by default; flip to print() if debugging the smoke job.
        pass

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        if self.path == "/health":
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 — stdlib method name
        if self.path == "/providers/cohere/v2/embed":
            body = self._body_json()
            texts = body.get("texts") or []
            vectors = [_embed_one(t) for t in texts]
            self._send_json({"embeddings": {"float": vectors}})
            return

        if self.path.startswith("/v1/chat/completions"):
            body = self._body_json()
            if body.get("stream"):
                self._stream_chat_completion()
            else:
                self._send_json({
                    "choices": [{"message": {"content": _SSE_ANSWER}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                })
            return

        self._send_json({"error": "not found", "path": self.path}, status=404)

    def _stream_chat_completion(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # A couple of word-level chunks so token streaming is genuinely
        # exercised, not just a single blob.
        words = _SSE_ANSWER.split(" ")
        for i, word in enumerate(words):
            chunk = word if i == len(words) - 1 else word + " "
            frame = {"choices": [{"delta": {"content": chunk}}]}
            self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), StubHandler)
    print(f"stub_backend: listening on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
