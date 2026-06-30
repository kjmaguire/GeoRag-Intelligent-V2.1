"""§19.3 Interpretation Workspace — CRUD for notes / section_lines /
target_zones / comments.

All routes are workspace-scoped via RLS (`set_config('app.workspace_id', $1, false)`).
Authentication uses the standard X-Service-Key + Bearer JWT contract.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.auth import UserContext, extract_user_context, verify_service_key
from app.services.workspace_resolution import resolve_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/interpretation",
    tags=["interpretation"],
    dependencies=[Depends(verify_service_key)],
)


# ─── Models ─────────────────────────────────────────────────────────
class NoteCreate(BaseModel):
    project_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    body_md: str = Field(..., min_length=1)
    anchor_geojson: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


class NoteRead(BaseModel):
    note_id: str
    project_id: str | None
    title: str | None
    body_md: str
    anchor_geojson: dict[str, Any] | None
    tags: list[str]
    author_user_id: int
    created_at: str
    updated_at: str


class SectionLineCreate(BaseModel):
    project_id: UUID | None = None
    name: str | None = Field(default=None, max_length=200)
    azimuth_deg: float | None = None
    geojson: dict[str, Any]  # LineString
    notes: str | None = None


class SectionLineRead(BaseModel):
    section_id: str
    project_id: str | None
    name: str | None
    azimuth_deg: float | None
    geojson: dict[str, Any]
    notes: str | None
    author_user_id: int
    created_at: str


class TargetZoneCreate(BaseModel):
    project_id: UUID | None = None
    name: str = Field(..., min_length=1, max_length=200)
    rationale: str | None = None
    commodity: str | None = None
    confidence: str = Field(default="medium")
    geojson: dict[str, Any]  # Polygon


class TargetZoneRead(BaseModel):
    zone_id: str
    project_id: str | None
    name: str
    rationale: str | None
    commodity: str | None
    confidence: str
    geojson: dict[str, Any]
    accepted: bool
    accepted_by: int | None
    accepted_at: str | None
    author_user_id: int
    created_at: str


class CommentCreate(BaseModel):
    project_id: UUID | None = None
    parent_comment_id: UUID | None = None
    target_table: str
    target_id: UUID
    body_md: str = Field(..., min_length=1)


class CommentRead(BaseModel):
    comment_id: str
    parent_comment_id: str | None
    target_table: str
    target_id: str
    body_md: str
    author_user_id: int
    created_at: str


_VALID_TARGET_TABLES = {
    "interpretation_notes",
    "interpretation_section_lines",
    "interpretation_target_zones",
}
_VALID_CONFIDENCES = {"low", "medium", "high"}


async def _scope(conn: asyncpg.Connection, workspace_id: UUID) -> None:
    """Set the RLS GUC for this connection."""
    await conn.execute(
        "SELECT set_config('app.workspace_id', $1, false)", str(workspace_id),
    )


def _jsonb(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _actor(user: UserContext) -> int:
    """Extract user_id from the JWT — default to 0 for system-level calls."""
    if user.user_id and str(user.user_id).isdigit():
        return int(user.user_id)
    return 0


# ─── Notes ──────────────────────────────────────────────────────────
@router.get("/notes", response_model=list[NoteRead])
async def list_notes(
    project_id: UUID | None = Query(default=None),
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> list[NoteRead]:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        where = "TRUE"
        params: list[Any] = []
        if project_id:
            where = "project_id = $1::uuid"
            params.append(project_id)
        rows = await conn.fetch(
            f"""
            SELECT note_id::text, project_id::text, title, body_md,
                   ST_AsGeoJSON(anchor_geom)::jsonb AS anchor_geojson,
                   tags, author_user_id, created_at, updated_at
              FROM interpretation.interpretation_notes
             WHERE {where}
             ORDER BY updated_at DESC
             LIMIT 500
            """,
            *params,
        )
    return [
        NoteRead(
            note_id=r["note_id"],
            project_id=r["project_id"],
            title=r["title"],
            body_md=r["body_md"],
            anchor_geojson=_jsonb(r["anchor_geojson"], None),
            tags=list(r["tags"] or []),
            author_user_id=int(r["author_user_id"]),
            created_at=r["created_at"].isoformat(),
            updated_at=r["updated_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("/notes", response_model=NoteRead, status_code=201)
async def create_note(
    body: NoteCreate,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> NoteRead:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)

    geom_json = json.dumps(body.anchor_geojson) if body.anchor_geojson else None
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        row = await conn.fetchrow(
            """
            INSERT INTO interpretation.interpretation_notes
                (workspace_id, project_id, author_user_id, title, body_md,
                 anchor_geom, tags)
            VALUES ($1::uuid, $2, $3, $4, $5,
                    CASE WHEN $6::text IS NULL THEN NULL
                         ELSE ST_SetSRID(ST_GeomFromGeoJSON($6::text), 4326) END,
                    $7::text[])
            RETURNING note_id::text, project_id::text, title, body_md,
                      ST_AsGeoJSON(anchor_geom)::jsonb AS anchor_geojson,
                      tags, author_user_id, created_at, updated_at
            """,
            workspace_id, body.project_id, _actor(user), body.title, body.body_md,
            geom_json, body.tags,
        )
    return NoteRead(
        note_id=row["note_id"],
        project_id=row["project_id"],
        title=row["title"],
        body_md=row["body_md"],
        anchor_geojson=_jsonb(row["anchor_geojson"], None),
        tags=list(row["tags"] or []),
        author_user_id=int(row["author_user_id"]),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(
    note_id: UUID,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> None:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        await conn.execute(
            "DELETE FROM interpretation.interpretation_notes WHERE note_id = $1::uuid",
            note_id,
        )


# ─── Section lines ──────────────────────────────────────────────────
@router.get("/section-lines", response_model=list[SectionLineRead])
async def list_section_lines(
    project_id: UUID | None = Query(default=None),
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> list[SectionLineRead]:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        where = "TRUE"
        params: list[Any] = []
        if project_id:
            where = "project_id = $1::uuid"
            params.append(project_id)
        rows = await conn.fetch(
            f"""
            SELECT section_id::text, project_id::text, name, azimuth_deg,
                   ST_AsGeoJSON(geom)::jsonb AS geojson, notes,
                   author_user_id, created_at
              FROM interpretation.interpretation_section_lines
             WHERE {where}
             ORDER BY created_at DESC
             LIMIT 500
            """,
            *params,
        )
    return [
        SectionLineRead(
            section_id=r["section_id"],
            project_id=r["project_id"],
            name=r["name"],
            azimuth_deg=float(r["azimuth_deg"]) if r["azimuth_deg"] else None,
            geojson=_jsonb(r["geojson"], {"type": "LineString", "coordinates": []}),
            notes=r["notes"],
            author_user_id=int(r["author_user_id"]),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("/section-lines", response_model=SectionLineRead, status_code=201)
async def create_section_line(
    body: SectionLineCreate,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> SectionLineRead:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    geom_json = json.dumps(body.geojson)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        row = await conn.fetchrow(
            """
            INSERT INTO interpretation.interpretation_section_lines
                (workspace_id, project_id, author_user_id, name, azimuth_deg,
                 geom, notes)
            VALUES ($1::uuid, $2, $3, $4, $5,
                    ST_SetSRID(ST_GeomFromGeoJSON($6::text), 4326),
                    $7)
            RETURNING section_id::text, project_id::text, name, azimuth_deg,
                      ST_AsGeoJSON(geom)::jsonb AS geojson, notes,
                      author_user_id, created_at
            """,
            workspace_id, body.project_id, _actor(user), body.name,
            body.azimuth_deg, geom_json, body.notes,
        )
    return SectionLineRead(
        section_id=row["section_id"],
        project_id=row["project_id"],
        name=row["name"],
        azimuth_deg=float(row["azimuth_deg"]) if row["azimuth_deg"] else None,
        geojson=_jsonb(row["geojson"], {}),
        notes=row["notes"],
        author_user_id=int(row["author_user_id"]),
        created_at=row["created_at"].isoformat(),
    )


@router.delete("/section-lines/{section_id}", status_code=204)
async def delete_section_line(
    section_id: UUID,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> None:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        await conn.execute(
            "DELETE FROM interpretation.interpretation_section_lines WHERE section_id = $1::uuid",
            section_id,
        )


# ─── Target zones ───────────────────────────────────────────────────
@router.get("/target-zones", response_model=list[TargetZoneRead])
async def list_target_zones(
    project_id: UUID | None = Query(default=None),
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> list[TargetZoneRead]:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        where = "TRUE"
        params: list[Any] = []
        if project_id:
            where = "project_id = $1::uuid"
            params.append(project_id)
        rows = await conn.fetch(
            f"""
            SELECT zone_id::text, project_id::text, name, rationale, commodity,
                   confidence, ST_AsGeoJSON(geom)::jsonb AS geojson,
                   accepted, accepted_by, accepted_at,
                   author_user_id, created_at
              FROM interpretation.interpretation_target_zones
             WHERE {where}
             ORDER BY created_at DESC
             LIMIT 500
            """,
            *params,
        )
    return [
        TargetZoneRead(
            zone_id=r["zone_id"],
            project_id=r["project_id"],
            name=r["name"],
            rationale=r["rationale"],
            commodity=r["commodity"],
            confidence=r["confidence"],
            geojson=_jsonb(r["geojson"], {}),
            accepted=bool(r["accepted"]),
            accepted_by=int(r["accepted_by"]) if r["accepted_by"] else None,
            accepted_at=r["accepted_at"].isoformat() if r["accepted_at"] else None,
            author_user_id=int(r["author_user_id"]),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("/target-zones", response_model=TargetZoneRead, status_code=201)
async def create_target_zone(
    body: TargetZoneCreate,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> TargetZoneRead:
    if body.confidence not in _VALID_CONFIDENCES:
        raise HTTPException(400, f"confidence must be one of {sorted(_VALID_CONFIDENCES)}")

    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    geom_json = json.dumps(body.geojson)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        row = await conn.fetchrow(
            """
            INSERT INTO interpretation.interpretation_target_zones
                (workspace_id, project_id, author_user_id, name, rationale,
                 commodity, confidence, geom)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7,
                    ST_SetSRID(ST_GeomFromGeoJSON($8::text), 4326))
            RETURNING zone_id::text, project_id::text, name, rationale, commodity,
                      confidence, ST_AsGeoJSON(geom)::jsonb AS geojson,
                      accepted, accepted_by, accepted_at,
                      author_user_id, created_at
            """,
            workspace_id, body.project_id, _actor(user), body.name, body.rationale,
            body.commodity, body.confidence, geom_json,
        )
    return TargetZoneRead(
        zone_id=row["zone_id"],
        project_id=row["project_id"],
        name=row["name"],
        rationale=row["rationale"],
        commodity=row["commodity"],
        confidence=row["confidence"],
        geojson=_jsonb(row["geojson"], {}),
        accepted=bool(row["accepted"]),
        accepted_by=int(row["accepted_by"]) if row["accepted_by"] else None,
        accepted_at=row["accepted_at"].isoformat() if row["accepted_at"] else None,
        author_user_id=int(row["author_user_id"]),
        created_at=row["created_at"].isoformat(),
    )


@router.post("/target-zones/{zone_id}/accept", response_model=TargetZoneRead)
async def accept_target_zone(
    zone_id: UUID,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> TargetZoneRead:
    """Mark a target zone as accepted (training-data capture for §21.4 feedback loop)."""
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        row = await conn.fetchrow(
            """
            UPDATE interpretation.interpretation_target_zones
               SET accepted = TRUE,
                   accepted_by = $1,
                   accepted_at = now()
             WHERE zone_id = $2::uuid
             RETURNING zone_id::text, project_id::text, name, rationale, commodity,
                       confidence, ST_AsGeoJSON(geom)::jsonb AS geojson,
                       accepted, accepted_by, accepted_at,
                       author_user_id, created_at
            """,
            _actor(user), zone_id,
        )
    if row is None:
        raise HTTPException(404, f"target_zone {zone_id} not found")
    return TargetZoneRead(
        zone_id=row["zone_id"],
        project_id=row["project_id"],
        name=row["name"],
        rationale=row["rationale"],
        commodity=row["commodity"],
        confidence=row["confidence"],
        geojson=_jsonb(row["geojson"], {}),
        accepted=bool(row["accepted"]),
        accepted_by=int(row["accepted_by"]) if row["accepted_by"] else None,
        accepted_at=row["accepted_at"].isoformat() if row["accepted_at"] else None,
        author_user_id=int(row["author_user_id"]),
        created_at=row["created_at"].isoformat(),
    )


@router.delete("/target-zones/{zone_id}", status_code=204)
async def delete_target_zone(
    zone_id: UUID,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> None:
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        await conn.execute(
            "DELETE FROM interpretation.interpretation_target_zones WHERE zone_id = $1::uuid",
            zone_id,
        )


# ─── Comments ───────────────────────────────────────────────────────
@router.get("/comments", response_model=list[CommentRead])
async def list_comments(
    target_table: str = Query(...),
    target_id: UUID = Query(...),
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> list[CommentRead]:
    if target_table not in _VALID_TARGET_TABLES:
        raise HTTPException(400, f"target_table must be one of {sorted(_VALID_TARGET_TABLES)}")
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        rows = await conn.fetch(
            """
            SELECT comment_id::text, parent_comment_id::text,
                   target_table, target_id::text,
                   body_md, author_user_id, created_at
              FROM interpretation.interpretation_comments
             WHERE target_table = $1 AND target_id = $2::uuid
             ORDER BY created_at ASC
            """,
            target_table, target_id,
        )
    return [
        CommentRead(
            comment_id=r["comment_id"],
            parent_comment_id=r["parent_comment_id"],
            target_table=r["target_table"],
            target_id=r["target_id"],
            body_md=r["body_md"],
            author_user_id=int(r["author_user_id"]),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("/comments", response_model=CommentRead, status_code=201)
async def create_comment(
    body: CommentCreate,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> CommentRead:
    if body.target_table not in _VALID_TARGET_TABLES:
        raise HTTPException(400, f"target_table must be one of {sorted(_VALID_TARGET_TABLES)}")
    pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pool, redis)
    async with pool.acquire() as conn:
        await _scope(conn, workspace_id)
        row = await conn.fetchrow(
            """
            INSERT INTO interpretation.interpretation_comments
                (workspace_id, project_id, author_user_id, parent_comment_id,
                 target_table, target_id, body_md)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7)
            RETURNING comment_id::text, parent_comment_id::text,
                      target_table, target_id::text,
                      body_md, author_user_id, created_at
            """,
            workspace_id, body.project_id, _actor(user),
            body.parent_comment_id, body.target_table, body.target_id, body.body_md,
        )
    return CommentRead(
        comment_id=row["comment_id"],
        parent_comment_id=row["parent_comment_id"],
        target_table=row["target_table"],
        target_id=row["target_id"],
        body_md=row["body_md"],
        author_user_id=int(row["author_user_id"]),
        created_at=row["created_at"].isoformat(),
    )


__all__ = ["router"]
