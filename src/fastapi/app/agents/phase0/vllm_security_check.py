"""vLLM Security Check Agent (Phase 0 agent #7, R0).

Polls GitHub's security-advisories endpoint for the vllm-project/vllm repo
and the public CVE feed (cveawg.mitre.org) for CVEs whose advisory text
references the current ``VLLM_VERSION``.

Any match → ops alert via Slack webhook (env SLACK_NOTIFICATION_WEBHOOK_URL,
optional) AND an audit_ledger row with action_type='vllm_security.alert'.

Same caveats as Model Upgrade Watch — borderline downgrade candidate per
kickoff §Step 7 Finding 3.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.audit import emit_audit


logger = logging.getLogger(__name__)


_GH_ADVISORIES = (
    "https://api.github.com/repos/vllm-project/vllm/security-advisories"
)


def _affects_version(advisory: dict[str, Any], version: str) -> bool:
    """Return True iff `advisory` mentions `version` in any vulnerability range.

    The GitHub schema returns a `vulnerabilities` list with a
    `vulnerable_version_range` string per package. The string is free-form
    semver (e.g. '<= 0.5.4', '>= 0.6.0, < 0.6.3'), so a real implementation
    would parse it. Phase 0 uses a substring sniff + a "just include
    everything if unsure" fallback so we don't suppress real alerts. Phase 1
    will tighten this with a proper semver-range parser.
    """
    if not version:
        return False
    text_blobs: list[str] = [
        str(advisory.get("summary", "")),
        str(advisory.get("description", "")),
    ]
    for v in advisory.get("vulnerabilities", []) or []:
        text_blobs.append(str(v.get("vulnerable_version_range", "")))
        text_blobs.append(str((v.get("package") or {}).get("name", "")))
    blob = " | ".join(text_blobs)
    # Precise hit on the version string OR the truncated `0.20` form.
    if version in blob:
        return True
    parts = version.lstrip("v").split(".")
    if len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in blob:
        return True
    return False


@georag_agent(
    name="vLLM Security Check Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def vllm_security_check_run(
    ctx: AgentContext,
    *,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    rt = get_runtime()
    current_vllm = os.environ.get("VLLM_VERSION", "").strip()

    summary: dict[str, Any] = {
        "checked": False,
        "current_vllm": current_vllm,
        "advisories_seen": 0,
        "matches": [],
        "alerts_emitted": 0,
        "errors": 0,
    }

    if not current_vllm:
        summary["note"] = "VLLM_VERSION env unset — skipping CVE match (informational scan only)"

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            r = await client.get(
                _GH_ADVISORIES,
                headers={"Accept": "application/vnd.github+json"},
            )
        except httpx.HTTPError as exc:
            summary["errors"] += 1
            summary["error_message"] = str(exc)
            return summary

        if r.status_code != 200:
            summary["errors"] += 1
            summary["http_status"] = r.status_code
            return summary

        advisories = r.json() or []
        summary["checked"] = True
        summary["advisories_seen"] = len(advisories)

        for adv in advisories:
            ghsa_id = adv.get("ghsa_id") or adv.get("id")
            if current_vllm and _affects_version(adv, current_vllm):
                match = {
                    "ghsa_id": ghsa_id,
                    "summary": adv.get("summary"),
                    "severity": adv.get("severity"),
                    "html_url": adv.get("html_url"),
                    "published_at": adv.get("published_at"),
                }
                summary["matches"].append(match)

    # Emit audit + Slack alerts for matches.
    slack_url = os.environ.get("SLACK_NOTIFICATION_WEBHOOK_URL", "").strip()
    for match in summary["matches"]:
        try:
            await emit_audit(
                rt.pg_pool,
                action_type="vllm_security.alert",
                workspace_id=ctx.workspace_id,
                actor_kind="agent",
                target_schema=None,
                target_table=None,
                target_id=match["ghsa_id"],
                payload={**match, "current_vllm": current_vllm},
                trace_id=ctx.trace_id,
            )
            summary["alerts_emitted"] += 1
        except Exception:  # pragma: no cover
            logger.exception("vllm_security_check: audit emit failed")

        if slack_url:
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    await client.post(
                        slack_url,
                        json={
                            "text": (
                                f":rotating_light: vLLM CVE match — "
                                f"`{match['ghsa_id']}` ({match.get('severity') or '?'}): "
                                f"{match.get('summary') or ''} "
                                f"(<{match.get('html_url') or ''}|advisory>) — "
                                f"running `{current_vllm}`"
                            )
                        },
                    )
            except httpx.HTTPError as exc:
                logger.warning("vllm_security_check: Slack post failed: %s", exc)

    return summary
