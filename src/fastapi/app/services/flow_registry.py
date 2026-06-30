"""Phase 4 Step 4 — DB-driven flow registry loader.

Reads ``workflow.flow_registry`` and resolves each row to a
``(workflow_object, input_model_class)`` tuple usable by
``integrations_trigger.py``.

Cache: a process-level dict with a 60s TTL. Adding a flow via SQL
becomes triggerable within one cache window, no fastapi restart needed.
Removing or disabling a flow takes effect on the next refresh too.

Failure mode: a registry row pointing at a module path that doesn't
exist (or an attribute that doesn't exist on the module) is logged
and skipped — one bad row doesn't break the whole registry.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Any

import asyncpg
from pydantic import BaseModel

log = logging.getLogger("georag.flow_registry")


CACHE_TTL_SECONDS = int(os.environ.get("FLOW_REGISTRY_CACHE_TTL", "60"))


class FlowEntry:
    """Resolved registry row: workflow + input model, plus metadata."""

    __slots__ = (
        "flow_name",
        "kind",
        "description",
        "flag_name",
        "enabled",
        "workflow",
        "input_model",
    )

    def __init__(
        self,
        flow_name: str,
        kind: str,
        description: str,
        flag_name: str | None,
        enabled: bool,
        workflow: Any,
        input_model: type[BaseModel],
    ) -> None:
        self.flow_name = flow_name
        self.kind = kind
        self.description = description
        self.flag_name = flag_name
        self.enabled = enabled
        self.workflow = workflow
        self.input_model = input_model


_cache_lock = threading.Lock()
_cache: dict[str, FlowEntry] = {}
_cache_loaded_at: float = 0.0


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _fetch_rows() -> list[dict]:
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT flow_name, kind, description, flag_name, enabled,
                   hatchet_workflow_module, hatchet_workflow_attr,
                   pydantic_input_attr
              FROM workflow.flow_registry
             WHERE enabled
            """
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _resolve_row(row: dict) -> FlowEntry | None:
    """Import the module and pull the workflow + input model objects.
    Returns None on any resolution error (logged)."""
    mod_path = row["hatchet_workflow_module"]
    wf_attr = row["hatchet_workflow_attr"]
    in_attr = row["pydantic_input_attr"]
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        log.warning(
            "flow_registry row '%s' — module '%s' not importable: %s",
            row["flow_name"], mod_path, e,
        )
        return None

    workflow = getattr(mod, wf_attr, None)
    input_model = getattr(mod, in_attr, None)
    if workflow is None or input_model is None:
        log.warning(
            "flow_registry row '%s' — module '%s' missing attrs (workflow=%s, input=%s)",
            row["flow_name"], mod_path, wf_attr, in_attr,
        )
        return None

    return FlowEntry(
        flow_name=row["flow_name"],
        kind=row["kind"],
        description=row["description"],
        flag_name=row["flag_name"],
        enabled=row["enabled"],
        workflow=workflow,
        input_model=input_model,
    )


async def get_registry(*, force_refresh: bool = False) -> dict[str, FlowEntry]:
    """Return the cached flow registry. Refreshes from the DB if the
    cache is older than the TTL (or on force_refresh)."""
    global _cache_loaded_at, _cache
    now = time.monotonic()
    with _cache_lock:
        cache_fresh = (
            _cache
            and not force_refresh
            and (now - _cache_loaded_at) < CACHE_TTL_SECONDS
        )
    if cache_fresh:
        return _cache

    try:
        rows = await _fetch_rows()
    except Exception as e:
        log.warning("flow_registry refresh failed (keeping prior cache): %s", e)
        return _cache

    resolved: dict[str, FlowEntry] = {}
    for row in rows:
        entry = _resolve_row(row)
        if entry is not None:
            resolved[row["flow_name"]] = entry

    with _cache_lock:
        _cache = resolved
        _cache_loaded_at = now
    log.info("flow_registry refreshed: %d flows resolved", len(resolved))
    return resolved


async def list_flow_names() -> list[str]:
    """Diagnostic shortcut for the integrations_trigger ``/flows`` endpoint."""
    registry = await get_registry()
    return sorted(registry.keys())


async def get_flow(flow_name: str) -> FlowEntry | None:
    registry = await get_registry()
    return registry.get(flow_name)


__all__ = [
    "FlowEntry",
    "get_registry",
    "get_flow",
    "list_flow_names",
    "CACHE_TTL_SECONDS",
]
