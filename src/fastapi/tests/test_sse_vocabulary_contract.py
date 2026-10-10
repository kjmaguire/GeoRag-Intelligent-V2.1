"""The SSE vocabulary is a contract between FastAPI, the Laravel relay and React.

POST /internal/queries streams exactly these frames:

    status · bind · delta · citation · completed · failed

tests/Unit/Jobs/SseVocabularyContractTest.php used to assert that a COMMENT line
with those six names was present in four files. FastAPI could add, drop or
rename a frame while the comment stayed, and the test stayed green. This file
derives the names from CODE instead:

* the set of event names `_agent_rag_stream` and the route's error handler can
  emit, read from the call sites of the two helpers that build a frame;
* the typed list the React consumer switches on;
* the placeholder source ids the React "no citations" warning must ignore,
  against the Python set it mirrors.

The Laravel relay's half (every name actually travels through it) is
SseVocabularyContractTest, now driven by SSE bytes through the job.

No server, no model.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from app.agent import response_assembler
from app.routers import queries

REPO = Path(__file__).resolve().parents[3]
QUERIES_PY = Path(queries.__file__)
CHAT_STREAM_TS = REPO / "resources" / "js" / "lib" / "chatStream.ts"

VOCABULARY = {"status", "bind", "delta", "citation", "completed", "failed"}

#: The two helpers that turn (event name, data) into a frame on the wire.
FRAME_BUILDERS = {"_stamped_event", "_sse_event"}


def _calls_to_frame_builders() -> list[tuple[ast.Call, str | None]]:
    """Every call to a frame builder, with the enclosing function's name."""
    tree = ast.parse(QUERIES_PY.read_text(encoding="utf-8"))
    found: list[tuple[ast.Call, str | None]] = []

    def visit(node: ast.AST, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            scope = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else enclosing
            if isinstance(child, ast.Call):
                callee = child.func
                name = callee.id if isinstance(callee, ast.Name) else None
                if name in FRAME_BUILDERS:
                    found.append((child, enclosing))
            visit(child, scope)

    visit(tree, None)
    return found


def emitted_event_names() -> set[str]:
    names: set[str] = set()
    for call, _scope in _calls_to_frame_builders():
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


# --------------------------------------------------------------------------- #
# Producer
# --------------------------------------------------------------------------- #


def test_the_stream_can_emit_exactly_the_documented_events():
    emitted = emitted_event_names()

    assert emitted == VOCABULARY, (
        f"routers/queries.py emits {sorted(emitted)}; the contract is {sorted(VOCABULARY)}. "
        "Changing a frame name breaks the Laravel relay and the React consumer, which wait "
        "for a terminal frame they will never see. Update all three sides together."
    )


def test_every_emit_site_names_its_event_with_a_literal():
    """So the check above cannot be dodged by passing the name in a variable.

    `_stamped_event` itself hands its own `event_name` parameter on to
    `_sse_event`; that one forwarding call is the only non-literal use.
    """
    dynamic = [
        (call.lineno, scope)
        for call, scope in _calls_to_frame_builders()
        if not (call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str))
        and scope != "_stamped_event"
    ]

    assert dynamic == [], f"frames built with a non-literal event name: {dynamic}"


def test_the_terminal_frames_are_still_emitted():
    """A stream with no terminal frame never ends for the browser."""
    emitted = emitted_event_names()
    assert {"completed", "failed"} <= emitted


def test_a_frame_is_event_line_json_data_line_blank_line():
    frame = queries._sse_event("failed", {"error": "x", "code": "TIMEOUT"})

    assert frame == 'event: failed\ndata: {"error": "x", "code": "TIMEOUT"}\n\n'
    name_line, data_line, blank, tail = frame.split("\n")
    assert name_line == "event: failed" and blank == "" and tail == ""
    assert json.loads(data_line.removeprefix("data: ")) == {"error": "x", "code": "TIMEOUT"}


# --------------------------------------------------------------------------- #
# Consumer
# --------------------------------------------------------------------------- #


def _ts_array(name: str) -> list[str]:
    """The string literals of `export const NAME = [ ... ] as const;` in chatStream.ts."""
    source = CHAT_STREAM_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}\s*=\s*\[(.*?)\]\s*as const", source, re.S)
    assert match, f"chatStream.ts no longer declares `export const {name} = [...] as const`"
    return re.findall(r"'([^']*)'", match.group(1))


def test_the_react_consumer_lists_the_same_events():
    assert set(_ts_array("SSE_VOCABULARY")) == VOCABULARY
    assert len(_ts_array("SSE_VOCABULARY")) == len(VOCABULARY), "a name is listed twice"


def test_the_react_placeholder_ids_mirror_the_assemblers():
    """The 'no citations' warning ignores these ids; the producer mints them.

    chatStream.ts copies the assembler's sentinel set because the browser cannot
    import Python. A copy drifts, and a drifted copy either hides the warning
    (an id it does not know counts as evidence) or raises it on a real answer.
    """
    assert set(_ts_array("EMPTY_SOURCE_SENTINELS")) == set(response_assembler.EMPTY_SOURCE_SENTINELS)
    assert tuple(_ts_array("EMPTY_SOURCE_SUFFIXES")) == response_assembler._EMPTY_SOURCE_SUFFIXES
    assert tuple(_ts_array("EMPTY_SOURCE_MARKERS")) == response_assembler._EMPTY_SOURCE_MARKERS
