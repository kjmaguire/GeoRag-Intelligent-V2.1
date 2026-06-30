"""Model Upgrade Watch Agent (Phase 0 agent #6, R0).

Polls upstream sources for newer vLLM releases / model checkpoints and
notifies operators when one is found.

Model identity is **env-driven**: the agent reads `LLM_PRIMARY_MODEL`
(or `VLLM_MODEL` as fallback) at invocation time. After the 2026-05-19
Qwen3-30B-A3B → Qwen3-14B-AWQ swap the agent automatically tracks the
new model id without any code or pin changes — verify by inspecting the
`summary["model"]["model_id"]` field of a recent run.

Sources (all best-effort; treat any HTTP error as "no signal" rather than
agent failure):
  - https://api.github.com/repos/vllm-project/vllm/releases/latest
  - https://huggingface.co/api/models/{LLM_PRIMARY_MODEL}

Notification:
  - audit_ledger row with action_type='model_upgrade.notification'
  - optional Slack webhook via env SLACK_NOTIFICATION_WEBHOOK_URL

Per kickoff §Step 7 Finding 3, this is a borderline downgrade candidate.
The notification leg may later be migrated onto Kestra (the integration-
boundary owner post-Activepieces sunset).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.audit import emit_audit

logger = logging.getLogger(__name__)


_VLLM_LATEST_RELEASE = "https://api.github.com/repos/vllm-project/vllm/releases/latest"


def _semver_tuple(s: str) -> tuple[int, ...]:
    """Parse a tag like 'v0.20.2' or '0.20.2-rc1' to a comparable tuple.

    Anything we can't parse returns (0,) so it sorts older-than-everything
    and we don't trigger false positives.
    """
    if not s:
        return (0,)
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if not m:
        return (0,)
    return tuple(int(g) for g in m.groups(default="0"))


@georag_agent(
    name="Model Upgrade Watch Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def model_upgrade_watch_run(
    ctx: AgentContext,
    *,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    rt = get_runtime()
    summary: dict[str, Any] = {
        "vllm": {"checked": False},
        "model": {"checked": False},
        "notifications_emitted": 0,
        "errors": 0,
    }

    current_vllm = os.environ.get("VLLM_VERSION", "").strip()
    current_model = (
        os.environ.get("LLM_PRIMARY_MODEL")
        or os.environ.get("VLLM_MODEL", "")
    ).strip()

    notifications: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        # ---- vLLM latest release -----------------------------------------
        try:
            r = await client.get(_VLLM_LATEST_RELEASE)
            if r.status_code == 200:
                payload = r.json()
                latest_tag = (payload.get("tag_name") or "").strip()
                summary["vllm"] = {
                    "checked": True,
                    "current": current_vllm,
                    "latest": latest_tag,
                    "url": payload.get("html_url"),
                }
                if (
                    current_vllm
                    and latest_tag
                    and _semver_tuple(latest_tag) > _semver_tuple(current_vllm)
                ):
                    notifications.append(
                        {
                            "kind": "vllm_release",
                            "current": current_vllm,
                            "latest": latest_tag,
                            "url": payload.get("html_url"),
                            "published_at": payload.get("published_at"),
                        }
                    )
            else:
                summary["vllm"] = {
                    "checked": True,
                    "http_status": r.status_code,
                    "current": current_vllm,
                }
        except (httpx.HTTPError, ValueError) as exc:
            summary["errors"] += 1
            summary["vllm"] = {"checked": False, "error": str(exc)}

        # ---- HuggingFace model card --------------------------------------
        if current_model:
            hf_url = f"https://huggingface.co/api/models/{current_model}"
            try:
                r = await client.get(hf_url)
                if r.status_code == 200:
                    payload = r.json()
                    # All-nighter 2026-05-21 — VRAM compatibility gate.
                    # The Phase 0 audit flagged that the agent would happily
                    # notify about candidates that physically can't fit on
                    # the host GPU. We read the safetensors total from the
                    # HF model card and compare it against
                    # GPU_VRAM_GB (default 20, A4500). The check uses ~60%
                    # of VRAM as the "model weights" budget — the remaining
                    # 40% is reserved for KV cache + scratch.
                    vram_gb = float(os.environ.get("GPU_VRAM_GB", "20"))
                    weights_budget_gb = vram_gb * 0.60
                    weights_bytes: int | None = None
                    safetensors = payload.get("safetensors") or {}
                    if isinstance(safetensors, dict):
                        # The HF API returns total bytes as `total` for
                        # safetensors-format models; older / non-st models
                        # have no field and we leave compatibility=unknown.
                        weights_bytes = safetensors.get("total")
                    compatibility: dict[str, Any]
                    if weights_bytes:
                        weights_gb = weights_bytes / (1024 ** 3)
                        compatibility = {
                            "weights_gb": round(weights_gb, 2),
                            "vram_gb": vram_gb,
                            "fits": weights_gb <= weights_budget_gb,
                            "headroom_gb": round(weights_budget_gb - weights_gb, 2),
                        }
                    else:
                        compatibility = {"weights_gb": None, "vram_gb": vram_gb, "fits": None}
                    summary["model"] = {
                        "checked": True,
                        "model_id": current_model,
                        "lastModified": payload.get("lastModified"),
                        "sha": payload.get("sha"),
                        "downloads": payload.get("downloads"),
                        "compatibility": compatibility,
                    }
                elif r.status_code == 404:
                    # 404 is informational, not a failure. The agent reads
                    # the model id from env (LLM_PRIMARY_MODEL / VLLM_MODEL)
                    # — if HF doesn't recognise the id, either the model is
                    # gated, the org slug is mistyped, or it's a community
                    # variant we haven't pulled yet. The Phase 0 dashboard
                    # surfaces the note string for the operator to check.
                    summary["model"] = {
                        "checked": True,
                        "model_id": current_model,
                        "http_status": 404,
                        "note": (
                            "model not found on HuggingFace — verify "
                            "LLM_PRIMARY_MODEL / VLLM_MODEL"
                        ),
                    }
                else:
                    summary["model"] = {
                        "checked": True,
                        "model_id": current_model,
                        "http_status": r.status_code,
                    }
            except (httpx.HTTPError, ValueError) as exc:
                summary["errors"] += 1
                summary["model"] = {"checked": False, "error": str(exc)}

    # All-nighter 2026-05-21 — annotate vLLM notifications with the model
    # VRAM check so operators don't act on a recommendation that physically
    # can't run on the host GPU.
    model_fits = summary.get("model", {}).get("compatibility", {}).get("fits")
    if model_fits is False:
        for notif in notifications:
            notif["vram_warning"] = (
                "current model weights exceed GPU_VRAM_GB * 0.60 budget — "
                "do NOT upgrade vLLM without first downsizing or quantising the model"
            )

    # ---- Emit notifications ----------------------------------------------
    slack_url = os.environ.get("SLACK_NOTIFICATION_WEBHOOK_URL", "").strip()
    for notif in notifications:
        try:
            await emit_audit(
                rt.pg_pool,
                action_type="model_upgrade.notification",
                workspace_id=ctx.workspace_id,
                actor_kind="agent",
                target_schema=None,
                target_table=None,
                target_id=notif["kind"],
                payload=notif,
                trace_id=ctx.trace_id,
            )
            summary["notifications_emitted"] += 1
        except Exception:  # pragma: no cover
            logger.exception("model_upgrade_watch: audit emit failed")

        if slack_url:
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    await client.post(
                        slack_url,
                        json={
                            "text": (
                                f":package: vLLM upgrade available — "
                                f"current `{notif['current']}`, "
                                f"latest `{notif['latest']}` "
                                f"(<{notif.get('url') or ''}|release notes>)"
                            )
                        },
                    )
            except httpx.HTTPError as exc:
                logger.warning("model_upgrade_watch: Slack post failed: %s", exc)
        elif notifications:
            logger.info(
                "model_upgrade_watch: SLACK_NOTIFICATION_WEBHOOK_URL unset — "
                "%d notification(s) recorded to audit_ledger only",
                len(notifications),
            )

    return summary
