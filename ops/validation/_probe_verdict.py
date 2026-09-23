"""Whether a probe run verified anything. Shared by both probes.

THE POINT, and it is worth stating once rather than twice. Every section of
every probe in this directory degrades instead of raising — deliberately, so
one dead section does not cost you the others. That means a run that fails
completely still writes a well-formed JSON file.

Before 2026-09-08 the Bedrock probe then printed "COMMIT THIS REPORT" and
exited 0 over a report whose every section was a 403. ADR-0022 makes that
file the gate on trusting the adapters, so a report of nothing but auth
failures would have satisfied the gate **by existing**. That is the same
shape as the other defects this migration turned up: not an error, just
something quietly not carrying the information it claims to.

Extracted here on 2026-09-15, when ADR-0023 added a second probe. Two copies
of this reasoning is how one of them loses it, and the copy that loses it is
the one nobody is looking at. The two probes differ in their section names
and in their auth-failure vocabulary — botocore error codes on one side, HTTP
statuses on the other — so those are parameters rather than a reason to fork
the logic.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["compute_verdict", "section_outcome"]


def section_outcome(section: Any) -> str:
    """Classify one report section as ``ok`` / ``failed`` / ``skipped``.

    The obvious version of this — "does the dict have an `error` key" — is
    wrong for the sections that fan out over request variants, and it was
    wrong in production. `probe_chat` returns

        {"model": "...",
         "without_response_format": {"error": ...},
         "with_response_format":    {"error": ...},
         "system_is_a_message":     {"error": ...}}

    Every call failed. The TOP-LEVEL dict has no `error` key, so a key check
    counted it as an observation and a run where nothing worked reported
    "verified 1/4 sections". That is the same absence-as-success shape this
    module exists to stop, one level down, and it was live in the Bedrock
    probe until 2026-09-15 — found by running the new Cohere probe against a
    fake server that 401s everything, not by reading either of them.

    So: if a section contains nested per-call results and EVERY one of them
    failed or skipped, the section failed. A section with no nested results
    (``latency`` is flat) is judged on its own keys, as before.

    And one more level of the same thing, found by the first run from inside
    the VPC (2026-09-23): a section needs at least one call that SUCCEEDED.
    Both Parse formats were refused with a 400 and the pixel ladder's only
    rung recorded ``{"status": 400, "accepted": False}`` — a rejection, but
    not an ``error`` — so the section read "ok" and the run printed
    "verified 4/4" over a Parse that had never once worked. A refused rung
    is an observation of the size limit only beside a rung that was
    accepted; alone, it is one more failed call.
    """
    if not isinstance(section, dict):
        return "failed" if section is None else "ok"
    if "error" in section:
        return "failed"
    if "skipped" in section:
        return "skipped"

    results = _collect_results(section)
    if results and not any(_succeeded(v) for v in results):
        return "skipped" if all("skipped" in v for v in results) else "failed"
    return "ok"


def _succeeded(result: dict) -> bool:
    """Whether one per-call result was a call the host actually served."""
    return not _failed(result) and "skipped" not in result and result.get("accepted") is not False


def _failed(result: dict) -> bool:
    """Whether one per-call result recorded a failure.

    An ``error`` key is the usual spelling. A call the host refused for its
    CREDENTIALS is the other: a rejection can be a legitimate observation (a
    Parse pixel-ladder rung refused for size is exactly what that ladder is
    for), but a 401/403 observes nothing about the model -- and a report
    that counted one as evidence once read "ok=parse" for a key Cohere had
    refused outright.
    """
    return "error" in result or result.get("code") == "AuthenticationError"


def _collect_results(section: dict) -> list[dict]:
    """Every per-call result a section recorded, at WHATEVER depth.

    The 2026-09-15 version of this looked exactly one level down, which was
    enough for `probe_chat` -- its variants sit directly on the section:

        {"without_response_format": {"error": ...}, ...}

    `probe_parse` groups its variants one level deeper:

        {"model": "parse-v5.0",
         "formats":      {"blocks": {"error": ...}, "markdown": {"error": ...}},
         "pixel_ladder": {"1900000": {"error": ...}}}

    `formats` and `pixel_ladder` are not results themselves and do not look
    like one, so a one-level scan found NO results, fell through to "ok",
    and reported a section in which every single call had failed as
    verified. On 2026-09-18 a real run proved it: every Cohere call was
    refused with a 403 and the probe printed "verified 1/4 sections
    (ok=parse)" and "COMMIT THIS REPORT" -- the precise outcome this module
    was extracted to make impossible, one more level down than the fix that
    created it.

    Recursing costs nothing: a group that holds no results contributes
    none, exactly as before, so a section carrying only config is still
    judged "ok" on its own keys rather than wrongly failed.
    """
    found: list[dict] = []
    for value in section.values():
        if not isinstance(value, dict):
            continue
        if "error" in value or "skipped" in value or _looks_like_a_result(value):
            found.append(value)
        else:
            found.extend(_collect_results(value))
    return found


def _looks_like_a_result(value: dict) -> bool:
    """A nested dict recording one attempted call, rather than metadata.

    Deliberately conservative: only dicts that clearly recorded an outcome
    count, so a section carrying a config block is not mistaken for a set of
    failed calls. The keys below are the union of what every section in
    either probe records on SUCCESS — being wrong in this direction costs a
    section wrongly marked failed, which is loud; being wrong in the other
    direction restores the bug this function exists for, which is silent.
    """
    return bool(
        value.keys()
        & {"latency_s", "status", "accepted", "total_s", "top_level_keys", "page0_keys"}
    )


def compute_verdict(
    report: dict[str, Any],
    *,
    sections: tuple[str, ...],
    is_auth_failure: Callable[[dict[str, Any]], bool],
    auth_hint: str,
) -> dict[str, Any]:
    """Classify each evidence section, and say plainly what was verified.

    ``sections`` is the list of names that count as evidence. A section
    ABSENT from the report is reported as ``missing`` rather than skipped
    over: it reads as a pass if you only test for ``error``/``skipped`` —
    the same absence-as-success shape this function exists to stop — and it
    is how adding a name to ``sections`` without wiring it up would quietly
    inflate the verified count.

    ``is_auth_failure`` is given each section dict and answers whether its
    failure was a credentials problem. Kept as a callback because "could not
    authenticate" is worth saying separately from "nothing worked", and the
    two probes recognise it by completely different fields.
    """
    failed: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    ok: list[str] = []

    for name in sections:
        if name not in report:
            missing.append(name)
            continue
        outcome = section_outcome(report[name] or {})
        {"failed": failed, "skipped": skipped, "ok": ok}[outcome].append(name)

    # An auth failure can be recorded on the section itself or on one of its
    # nested per-call results — `probe_chat` puts it on the latter — so both
    # are asked. Missing this makes a run whose every call was a 401 report
    # "no section produced an observation", which sends the operator to read
    # an adapter when the answer is that the key is wrong.
    saw_auth_failure = any(
        is_auth_failure(report.get(name) or {})
        or any(
            isinstance(value, dict) and is_auth_failure(value)
            for value in (report.get(name) or {}).values()
        )
        for name in sections
    )

    if saw_auth_failure and not ok:
        summary = f"could not authenticate; nothing was observed. {auth_hint}"
    elif not ok:
        summary = "no section produced an observation."
    else:
        summary = (
            f"verified {len(ok)}/{len(sections)} sections "
            f"(ok={','.join(ok) or '-'}; "
            f"failed={','.join(failed) or '-'}; "
            f"skipped={','.join(skipped) or '-'}"
            + (f"; MISSING={','.join(missing)}" if missing else "")
            + ")."
        )

    return {
        "verified_anything": bool(ok),
        "sections_ok": ok,
        "sections_failed": failed,
        "sections_skipped": skipped,
        "sections_missing": missing,
        "authentication_failed": saw_auth_failure,
        "summary": summary,
    }
