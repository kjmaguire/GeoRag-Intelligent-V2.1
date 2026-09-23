"""A Cohere-shaped server, so the probe can be exercised without a key.

WHY THIS EXISTS
    `cohere_probe.py` has no test coverage from running it against the real
    API — that costs a credential and a live call. What it CAN have is proof
    that it reads a response correctly, and that it reports the one failure
    it was written to catch. This server is that.

    It paid for itself on the first run. Two real defects in the probe turned
    up that no amount of reading would have found:

      1. The probe imported `_delta_text` from `llm_cohere` inside a bare
         `except: return None`, so a run from a shell without
         FASTAPI_SERVICE_KEY — `app.config` builds Settings at import —
         printed "_delta_text read NOTHING <-- adapter is wrong" over a
         stream it had just parsed correctly. A probe accusing the code it
         exists to check is worse than one that says nothing: the operator
         goes and edits a working adapter.
      2. Every Parse response field was reported as UNDECLARED, because the
         contract carried twelve declared fields and no `evidence_key` for
         any of them. The Bedrock version had the same hole; its parse diff
         could only ever say "not_observed".

    Neither is the kind of bug a reviewer catches. Both are the kind a single
    live call catches, which is the entire thesis of the probe itself.

MODES
    Set `FAKE_COHERE_MODE` before starting:

      honest   (default) role:'system' is honoured, JSON mode works, the
               sentinels are present — the shape the adapters assume.
      ignores_system
               200 OK, fluent answer, system prompt silently dropped. THE
               failure this probe exists for: nothing errors, the answer
               looks fine, and the grounding rules were never applied while
               the citation guards go on enforcing against the output.
      strips_sentinels
               JSON arrives unwrapped, which would make
               `clean_model_text`'s stripping dead code on this host.
      unauthorized
               401 on everything, so the verdict's "could not authenticate"
               path can be exercised.
      unreadable_stream
               200 to a streaming call with a body in which no line is an
               event — the shape of the 2026-09-23 live run, whose stream
               section reported `event_types: {}` and still read "ok".
      ndjson_stream / whole_body_stream
               The two other framings a 200 stream could have arrived in:
               one JSON event per line, or one complete (pretty-printed)
               non-streaming reply. The adapter must read both.

WHAT IS FROM A LIVE CALL (2026-09-23, from inside the VPC), not a guess:
    * non-streaming replies carry `message.role` and content blocks keyed
      {type, text, thinking} — reasoning is on by default;
    * Parse refuses `document.image_url` as an object, with a 400 and the
      exact message below.
    The Parse RESPONSE and the streaming framing are still the SDK's word
    (cohere 7.1.1), not a live observation.

Run directly (`python fake_cohere.py`) or import `serve()` from a test.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

CANARY = "GROUNDED"
JSON_ANSWER = '{"ok": true, "unit": "ppm"}'


def _mode() -> str:
    return os.environ.get("FAKE_COHERE_MODE", "honest")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D102 — quiet
        pass

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        if _mode() == "unauthorized":
            self._json({"message": "invalid api token"}, 401)
        elif self.path == "/v1/models":
            self._json(
                {
                    "models": [
                        {"name": "command-a-plus-05-2026"},
                        {"name": "parse-v5.0"},
                    ]
                }
            )
        else:
            self._json({"message": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        if _mode() == "unauthorized":
            self._json({"message": "invalid api token"}, 401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v2/parse":
            self._parse(body)
        elif body.get("stream"):
            self._stream()
        else:
            self._chat(body)

    # -- responses ---------------------------------------------------------

    def _chat(self, body: dict) -> None:
        roles = [m.get("role") for m in body.get("messages") or []]
        honours_system = "system" in roles and _mode() != "ignores_system"

        if honours_system:
            text = CANARY
        elif body.get("response_format"):
            text = JSON_ANSWER
            if _mode() != "strips_sentinels":
                text = f"<|START_TEXT|>{text}<|END_TEXT|>"
        else:
            text = "Sure! Here is some prose about drill grades."

        self._json(
            {
                "id": "fake",
                "finish_reason": "COMPLETE",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "The user wants a reply."},
                        {"type": "text", "text": text},
                    ],
                },
                "usage": {"tokens": {"input_tokens": 11, "output_tokens": 3}},
            }
        )

    def _stream(self) -> None:
        mode = _mode()
        frames = [
            {"type": "message-start", "id": "fake", "delta": {"message": {"role": "assistant"}}},
            {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"thinking": "Count."}}}},
            *(
                {"type": "content-delta", "index": 1, "delta": {"message": {"content": {"text": piece}}}}
                for piece in ("one ", "two ", "three")
            ),
            {
                "type": "message-end",
                "delta": {
                    "finish_reason": "COMPLETE",
                    "usage": {"tokens": {"input_tokens": 9, "output_tokens": 3}},
                },
            },
        ]
        if mode == "whole_body_stream":
            self._json(
                {
                    "id": "fake",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "one two three"}]},
                    "usage": {"tokens": {"input_tokens": 9, "output_tokens": 3}},
                },
                indent=2,
            )
            return
        self.send_response(200)
        if mode == "unreadable_stream":
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"one two three\n")
            return
        if mode == "ndjson_stream":
            self.send_header("Content-Type", "application/stream+json")
            self.end_headers()
            for frame in frames:
                self.wfile.write(f"{json.dumps(frame)}\n".encode())
            return
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in frames:
            self.wfile.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _parse(self, body: dict) -> None:
        document = body.get("document") or {}
        if not isinstance(document.get("image_url"), str):
            # Verbatim from the 2026-09-23 live run, id aside.
            self._json(
                {
                    "id": "fake",
                    "message": "invalid type: parameter 'document.image_url' is of type "
                    f"{type(document.get('image_url')).__name__.replace('dict', 'object')} "
                    "but should be of type string",
                },
                400,
            )
            return
        if body.get("output_format") == "markdown":
            self._json(
                {
                    "id": "fake",
                    "pages": [
                        {
                            "type": "markdown",
                            "index": 0,
                            "markdown": {"content": "# Collar table\n\n| hole | m |\n", "images": []},
                        }
                    ],
                }
            )
            return
        box = {"x": 0, "y": 0, "width": 1, "height": 1}
        self._json(
            {
                "id": "fake",
                "pages": [
                    {
                        "type": "blocks",
                        "index": 0,
                        "blocks": [
                            {"type": "text", "text": {"content": "DDH-24-001 collar log"}},
                            {
                                "type": "table",
                                "table": {
                                    "type": "html",
                                    "html": "<table><tr><td>1.2</td></tr></table>",
                                    "bounding_box": box,
                                    "bounding_box_normalized": box,
                                },
                            },
                        ],
                    }
                ],
            }
        )

    def _json(self, payload: dict, status: int = 200, *, indent: int | None = None) -> None:
        raw = json.dumps(payload, indent=indent).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(port: int = 0) -> tuple[HTTPServer, threading.Thread]:
    """Start on ``port`` (0 = any free one) and return the server and thread.

    Port 0 by default so parallel test runs cannot collide on a fixed one —
    read the real port back from ``server.server_address[1]``.
    """
    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


if __name__ == "__main__":
    srv, _ = serve(int(os.environ.get("FAKE_COHERE_PORT", "8799")))
    print(f"fake cohere on http://127.0.0.1:{srv.server_address[1]} mode={_mode()}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
