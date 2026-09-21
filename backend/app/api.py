import hashlib
import io
import json
import math
from datetime import datetime
from pathlib import PurePath
from typing import Any
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError, field_validator

from backend.app.config import settings
from backend.app.db import connection
from backend.app.domain.models import JobStatus
from backend.app.domain.planning import PlanningRequest, PlanningResult, validate_plan
from backend.app.queue import enqueue
from backend.app.render import probe_duration
from backend.app.storage.local import LocalStorage

app = FastAPI(title="Hype AI Studio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProjectCreate(BaseModel):
    project_type: str = "MUSIC_VIDEO"
    title: str
    creative_brief: str = ""
    aspect_ratio: str = "16:9"


class ShotCreate(BaseModel):
    ordinal: int = Field(ge=1)
    title: str = ""
    prompt: str
    intended_duration: float = Field(gt=0, le=60)


class ShotUpdate(ShotCreate):
    pass


class ShotOrder(BaseModel):
    shot_ids: list[UUID] = Field(min_length=1)


class IntroCard(BaseModel):
    artist_name: str = Field(min_length=1, max_length=100)
    song_title: str = Field(min_length=1, max_length=150)
    duration_seconds: float = Field(ge=1, le=10, allow_inf_nan=False)

    @field_validator("artist_name", "song_title")
    @classmethod
    def card_text_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("card text must not be blank")
        return value


class OutroCard(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    duration_seconds: float = Field(ge=1, le=10, allow_inf_nan=False)

    @field_validator("text")
    @classmethod
    def card_text_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("card text must not be blank")
        return value


class RenderCreate(BaseModel):
    variant_ids: list[UUID] = Field(min_length=1)
    audio_asset_id: UUID
    audio_start_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    intro_card: IntroCard | None = None
    outro_card: OutroCard | None = None


class PlanGenerate(BaseModel):
    target_duration_seconds: float = Field(gt=0, le=600)
    visual_tone: str = Field(min_length=1)
    narrative_approach: str = Field(min_length=1)
    performance_presence: str = Field(min_length=1)
    pacing: str = Field(min_length=1)
    constraints: str = ""
    use_reference_assets: bool = False
    maximum_shot_count: int = Field(default=12, ge=1, le=48)
    idempotency_key: str = Field(min_length=1, max_length=200)


class PlanVersionCreate(PlanningResult):
    pass


class PlanSummary(BaseModel):
    id: UUID
    version: int
    status: str
    concept_title: str
    provider: str
    model: str
    created_at: datetime
    approved_at: datetime | None


class PlanApprovalResponse(BaseModel):
    plan_id: UUID
    status: str
    created_shot_ids: list[UUID]
    idempotent: bool


class PlanJobResponse(BaseModel):
    job_id: UUID
    status: str
    deduplicated: bool


class PlanDetail(PlanningResult):
    id: UUID
    project_id: UUID
    version: int
    status: str
    parent_plan_id: UUID | None
    planning_inputs: dict[str, Any]
    provider: str
    model: str
    source_job_id: UUID | None
    created_at: datetime
    approved_at: datetime | None
    materialized_at: datetime | None
    provider_evidence: dict[str, Any] | None = None


def record_event(cursor, project_id, event_type, entity_type, entity_id, payload=None):
    cursor.execute(
        "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) VALUES (%s,%s,'api',%s,%s,%s)",
        (project_id, event_type, entity_type, entity_id, Jsonb(payload or {})),
    )


def media_response(storage_key: str, mime_type: str, download: bool) -> FileResponse:
    path = LocalStorage(settings.storage_root).path(storage_key)
    if not path.is_file():
        raise HTTPException(404, "media not found")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type=mime_type,
        filename=path.name,
        content_disposition_type=disposition,
    )


@app.get("/projects")
def list_projects():
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id,project_type,title,creative_brief,aspect_ratio,status,created_at,updated_at "
            "FROM projects WHERE project_type='MUSIC_VIDEO' ORDER BY updated_at DESC,created_at DESC"
        )
        rows = cursor.fetchall()
    fields = (
        "id",
        "project_type",
        "title",
        "creative_brief",
        "aspect_ratio",
        "status",
        "created_at",
        "updated_at",
    )
    return [dict(zip(fields, row)) for row in rows]


@app.post("/projects", status_code=201)
def create_project(body: ProjectCreate):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO projects(project_type,title,creative_brief,aspect_ratio) VALUES (%s,%s,%s,%s) RETURNING id",
            (body.project_type, body.title, body.creative_brief, body.aspect_ratio),
        )
        project_id = cursor.fetchone()[0]
        record_event(cursor, project_id, "PROJECT_CREATED", "project", project_id)
        conn.commit()
    return {"id": str(project_id), **body.model_dump()}


@app.get("/projects/{project_id}")
def get_project(project_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id,project_type,title,creative_brief,aspect_ratio,status,created_at,updated_at FROM projects WHERE id=%s",
            (project_id,),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "SELECT (SELECT count(*) FROM assets WHERE project_id=%s),"
                "(SELECT count(*) FROM shots WHERE project_id=%s),"
                "(SELECT count(*) FROM shots WHERE project_id=%s AND selected_variant_id IS NOT NULL),"
                "(SELECT count(*) FROM renders WHERE project_id=%s)",
                (project_id, project_id, project_id, project_id),
            )
            counts = cursor.fetchone()
    if not row:
        raise HTTPException(404, "project not found")
    result = dict(
        zip(
            (
                "id",
                "project_type",
                "title",
                "creative_brief",
                "aspect_ratio",
                "status",
                "created_at",
                "updated_at",
            ),
            row,
        )
    )
    result["summary"] = {
        "assets": counts[0],
        "shots": counts[1],
        "selected_variants": counts[2],
        "renders": counts[3],
    }
    return result


@app.post("/projects/{project_id}/assets", status_code=201)
def upload_asset(
    project_id: UUID,
    file: UploadFile = File(...),  # noqa: B008
    asset_type: str = Form("AUDIO"),
    rights_metadata: str = Form("{}"),
):
    filename = file.filename or ""
    if not filename or PurePath(filename).name != filename:
        raise HTTPException(400, "unsafe or missing filename")
    data = file.file.read(settings.max_asset_upload_bytes + 1)
    if not data:
        raise HTTPException(400, "uploaded file is empty")
    if len(data) > settings.max_asset_upload_bytes:
        raise HTTPException(413, "uploaded file exceeds the size limit")
    try:
        rights = json.loads(rights_metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "rights_metadata must be valid JSON") from exc
    if not isinstance(rights, dict):
        raise HTTPException(422, "rights_metadata must be a JSON object")
    key = f"projects/{project_id}/assets/{filename}"
    checksum = hashlib.sha256(data).hexdigest()
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM projects WHERE id=%s", (project_id,))
        if cursor.fetchone() is None:
            raise HTTPException(404, "project not found")
        LocalStorage(settings.storage_root).save(key, data)
        cursor.execute(
            "INSERT INTO assets(project_id,asset_type,storage_key,mime_type,size_bytes,checksum,rights_metadata) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                project_id,
                asset_type,
                key,
                file.content_type or "application/octet-stream",
                len(data),
                checksum,
                Jsonb(rights),
            ),
        )
        asset_id = cursor.fetchone()[0]
        record_event(
            cursor,
            project_id,
            "ASSET_UPLOADED",
            "asset",
            asset_id,
            {"asset_type": asset_type, "filename": filename},
        )
        conn.commit()
    return {"id": str(asset_id), "storage_key": key, "checksum": checksum}


@app.get("/projects/{project_id}/assets")
def list_assets(project_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM projects WHERE id=%s", (project_id,))
        if cursor.fetchone() is None:
            raise HTTPException(404, "project not found")
        cursor.execute(
            "SELECT id,asset_type,storage_key,mime_type,size_bytes,checksum,rights_metadata,created_at "
            "FROM assets WHERE project_id=%s ORDER BY created_at",
            (project_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "id": str(row[0]),
            "asset_type": row[1],
            "filename": PurePath(row[2]).name,
            "mime_type": row[3],
            "size_bytes": row[4],
            "checksum": row[5],
            "rights_metadata": row[6],
            "created_at": row[7],
        }
        for row in rows
    ]


@app.get("/assets/{asset_id}/media")
def get_asset_media(asset_id: UUID, download: bool = Query(False)):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT storage_key,mime_type FROM assets WHERE id=%s", (asset_id,))
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, "asset not found")
    return media_response(row[0], row[1], download)


@app.post("/projects/{project_id}/shots", status_code=201)
def create_shot(project_id: UUID, body: ShotCreate):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM projects WHERE id=%s", (project_id,))
        if cursor.fetchone() is None:
            raise HTTPException(404, "project not found")
        cursor.execute(
            "INSERT INTO shots(project_id,ordinal,title,prompt,intended_duration) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (project_id, body.ordinal, body.title, body.prompt, body.intended_duration),
        )
        shot_id = cursor.fetchone()[0]
        conn.commit()
    return {"id": str(shot_id), **body.model_dump()}


@app.get("/projects/{project_id}/shots")
def list_shots(project_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT s.id,s.ordinal,s.title,s.prompt,s.intended_duration,s.status,s.selected_variant_id,"
            "s.source_plan_id,s.source_plan_item_key,"
            "j.id,j.status FROM shots s LEFT JOIN LATERAL "
            "(SELECT id,status FROM jobs WHERE shot_id=s.id ORDER BY created_at DESC LIMIT 1) j ON true "
            "WHERE s.project_id=%s ORDER BY s.ordinal",
            (project_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "id": str(row[0]),
            "ordinal": row[1],
            "title": row[2],
            "prompt": row[3],
            "intended_duration": float(row[4]),
            "status": row[5],
            "selected_variant_id": str(row[6]) if row[6] else None,
            "source_plan_id": str(row[7]) if row[7] else None,
            "source_plan_item_key": row[8],
            "latest_job_id": str(row[9]) if row[9] else None,
            "latest_job_status": row[10],
        }
        for row in rows
    ]


@app.put("/shots/{shot_id}")
def update_shot(shot_id: UUID, body: ShotUpdate):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "UPDATE shots SET ordinal=%s,title=%s,prompt=%s,intended_duration=%s,updated_at=now() "
            "WHERE id=%s RETURNING id",
            (body.ordinal, body.title, body.prompt, body.intended_duration, shot_id),
        )
        if cursor.fetchone() is None:
            raise HTTPException(404, "shot not found")
        conn.commit()
    return {"id": str(shot_id), **body.model_dump()}


@app.put("/projects/{project_id}/shots/order")
def reorder_shots(project_id: UUID, body: ShotOrder):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM shots WHERE project_id=%s ORDER BY ordinal FOR UPDATE", (project_id,)
        )
        existing = [row[0] for row in cursor.fetchall()]
        if set(existing) != set(body.shot_ids) or len(existing) != len(body.shot_ids):
            raise HTTPException(422, "shot_ids must contain every project shot exactly once")
        cursor.execute("UPDATE shots SET ordinal=-ordinal WHERE project_id=%s", (project_id,))
        for ordinal, shot_id in enumerate(body.shot_ids, start=1):
            cursor.execute(
                "UPDATE shots SET ordinal=%s,updated_at=now() WHERE id=%s", (ordinal, shot_id)
            )
        conn.commit()
    return {"shot_ids": [str(value) for value in body.shot_ids]}


@app.post("/shots/{shot_id}/generations", status_code=202)
def submit_generation(shot_id: UUID, idempotency_key: str, reference_asset_id: UUID | None = None):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id,project_id FROM shots WHERE id=%s", (shot_id,))
        shot = cursor.fetchone()
        if not shot:
            raise HTTPException(404, "shot not found")
        request_data: dict[str, Any] = {}
        if reference_asset_id:
            cursor.execute(
                "SELECT project_id,asset_type,storage_key,mime_type,size_bytes,checksum,rights_metadata "
                "FROM assets WHERE id=%s",
                (reference_asset_id,),
            )
            asset = cursor.fetchone()
            if asset is None:
                raise HTTPException(404, "reference image asset not found")
            if asset[0] != shot[1]:
                raise HTTPException(422, "reference image must belong to the shot project")
            if asset[1] != "REFERENCE_IMAGE":
                raise HTTPException(422, "reference asset must be an image")
            if asset[3] not in {"image/jpeg", "image/png", "image/webp"}:
                raise HTTPException(422, "reference image must be JPEG, PNG, or WebP")
            rights = asset[6] or {}
            if rights.get("usage_confirmed") is not True:
                raise HTTPException(422, "reference image usage rights must be confirmed")
            if (
                rights.get("depicts_real_person") is True
                and rights.get("likeness_consent_confirmed") is not True
            ):
                raise HTTPException(422, "likeness consent must be confirmed for a real person")
            path = LocalStorage(settings.storage_root).path(asset[2])
            try:
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != asset[5]:
                    raise HTTPException(422, "reference image checksum does not match")
                with Image.open(io.BytesIO(data)) as image:
                    detected_mime = Image.MIME.get(image.format or "")
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
            except (OSError, UnidentifiedImageError) as exc:
                raise HTTPException(422, "reference image content is invalid") from exc
            if detected_mime != asset[3]:
                raise HTTPException(422, "reference image MIME type does not match its content")
            aspect_ratio = width / height
            if not 0.5 <= aspect_ratio <= 2:
                raise HTTPException(422, "reference image aspect ratio must be between 0.5 and 2")
            request_data["reference_image"] = {
                "asset_id": str(reference_asset_id),
                "checksum": asset[5],
                "storage_key": asset[2],
                "mime_type": asset[3],
                "width": width,
                "height": height,
                "center_crop_warning": abs(aspect_ratio - (16 / 9)) > 0.01,
            }
            derived_from = rights.get("derived_from_asset_id")
            if derived_from:
                cursor.execute(
                    "SELECT 1 FROM assets WHERE id=%s AND project_id=%s "
                    "AND asset_type='REFERENCE_IMAGE'",
                    (derived_from, shot[1]),
                )
                if cursor.fetchone() is None:
                    raise HTTPException(422, "derived image provenance source is invalid")
                request_data["reference_image"]["derived_from_asset_id"] = str(derived_from)
        cursor.execute(
            "INSERT INTO jobs(project_id,shot_id,job_type,status,idempotency_key,max_retries,request_data) "
            "VALUES (%s,%s,'VIDEO_GENERATION','QUEUED',%s,%s,%s) ON CONFLICT (idempotency_key) "
            "DO NOTHING RETURNING id,status",
            (shot[1], shot_id, idempotency_key, settings.max_retries, Jsonb(request_data)),
        )
        created = cursor.fetchone()
        if created is None:
            cursor.execute(
                "SELECT id,status,shot_id,request_data FROM jobs WHERE idempotency_key=%s",
                (idempotency_key,),
            )
            existing = cursor.fetchone()
            if existing[2] != shot_id or existing[3] != request_data:
                raise HTTPException(409, "idempotency key belongs to another request")
            conn.commit()
            return {"job_id": str(existing[0]), "status": existing[1], "deduplicated": True}
        job_id = created[0]
        record_event(
            cursor,
            shot[1],
            "JOB_SUBMITTED",
            "job",
            job_id,
            {
                "job_type": "VIDEO_GENERATION",
                "generation_mode": "image-to-video" if reference_asset_id else "text-to-video",
                "reference_asset_id": str(reference_asset_id) if reference_asset_id else None,
            },
        )
        conn.commit()
    enqueue(str(job_id))
    return {"job_id": str(job_id), "status": JobStatus.QUEUED, "deduplicated": False}


@app.get("/jobs/{job_id}")
def get_job(job_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id,status,retry_count,error_data,created_at,started_at,completed_at FROM jobs WHERE id=%s",
            (job_id,),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return dict(
        zip(
            (
                "id",
                "status",
                "retry_count",
                "error_data",
                "created_at",
                "started_at",
                "completed_at",
            ),
            row,
        )
    )


@app.get("/shots/{shot_id}/variants")
def variants(shot_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT v.id,v.storage_key,v.mime_type,v.duration,v.review_status,v.created_at,v.generation_attempt_id,"
            "a.attempt_number,a.job_id,a.parameters,a.provider,a.model FROM shot_variants v JOIN generation_attempts a ON a.id=v.generation_attempt_id "
            "WHERE v.shot_id=%s ORDER BY v.created_at",
            (shot_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "id": str(r[0]),
            "storage_key": r[1],
            "mime_type": r[2],
            "duration": float(r[3]),
            "review_status": r[4],
            "created_at": r[5],
            "generation_attempt_id": str(r[6]),
            "attempt_number": r[7],
            "job_id": str(r[8]),
            "provenance": r[9],
            "provider": r[10],
            "model": r[11],
            "generation_mode": (
                (r[9] or {}).get("generation_mode")
                or ("image-to-video" if (r[9] or {}).get("reference_asset_id") else "text-to-video")
            ),
        }
        for r in rows
    ]


@app.post("/shots/{shot_id}/variants/{variant_id}/select")
def select_variant(shot_id: UUID, variant_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT project_id FROM shots WHERE id=%s AND EXISTS (SELECT 1 FROM shot_variants WHERE id=%s AND shot_id=%s)",
            (shot_id, variant_id, shot_id),
        )
        project = cursor.fetchone()
        if not project:
            raise HTTPException(404, "variant not found")
        cursor.execute(
            "UPDATE shot_variants SET review_status='SUPERSEDED' WHERE shot_id=%s AND review_status='SELECTED'",
            (shot_id,),
        )
        cursor.execute(
            "UPDATE shot_variants SET review_status='SELECTED' WHERE id=%s", (variant_id,)
        )
        cursor.execute("UPDATE shots SET selected_variant_id=%s WHERE id=%s", (variant_id, shot_id))
        record_event(cursor, project[0], "VARIANT_SELECTED", "shot_variant", variant_id)
        conn.commit()
    return {"variant_id": str(variant_id), "review_status": "SELECTED"}


@app.post("/shots/{shot_id}/variants/{variant_id}/reject")
def reject_variant(shot_id: UUID, variant_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT s.project_id,v.review_status FROM shots s JOIN shot_variants v ON v.shot_id=s.id "
            "WHERE s.id=%s AND v.id=%s FOR UPDATE",
            (shot_id, variant_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404, "variant not found")
        cursor.execute(
            "UPDATE shot_variants SET review_status='REJECTED' WHERE id=%s", (variant_id,)
        )
        cursor.execute(
            "UPDATE shots SET selected_variant_id=NULL WHERE id=%s AND selected_variant_id=%s",
            (shot_id, variant_id),
        )
        if row[1] != "REJECTED":
            record_event(cursor, row[0], "VARIANT_REJECTED", "shot_variant", variant_id)
        conn.commit()
    return {"variant_id": str(variant_id), "review_status": "REJECTED"}


@app.get("/variants/{variant_id}/media")
def get_variant_media(variant_id: UUID, download: bool = Query(False)):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT storage_key,mime_type FROM shot_variants WHERE id=%s", (variant_id,))
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, "variant not found")
    return media_response(row[0], row[1], download)


def _plan_detail(row, provider_evidence=None) -> dict:
    return {
        "id": str(row[0]),
        "project_id": str(row[1]),
        "version": row[2],
        "status": row[3],
        "parent_plan_id": str(row[4]) if row[4] else None,
        "planning_inputs": row[5],
        "schema_version": row[11],
        "concept_title": row[6],
        "logline": row[7],
        "treatment": row[8],
        "creative_direction": row[9],
        "shots": row[10],
        "provider": row[12],
        "model": row[13],
        "source_job_id": str(row[14]) if row[14] else None,
        "created_at": row[15],
        "approved_at": row[16],
        "materialized_at": row[17],
        "provider_evidence": provider_evidence,
    }


_PLAN_COLUMNS = (
    "id,project_id,version,status,parent_plan_id,planning_inputs,concept_title,logline,treatment,"
    "creative_direction,shot_plan,prompt_schema_version,provider,model,source_job_id,created_at,approved_at,materialized_at"
)


@app.post("/projects/{project_id}/plans/generate", status_code=202, response_model=PlanJobResponse)
def generate_plan(project_id: UUID, body: PlanGenerate):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT creative_brief,aspect_ratio,title FROM projects WHERE id=%s AND project_type='MUSIC_VIDEO'",
            (project_id,),
        )
        project = cursor.fetchone()
        if project is None:
            raise HTTPException(404, "music video project not found")
        assets = []
        if body.use_reference_assets:
            cursor.execute(
                "SELECT id,asset_type,mime_type,size_bytes,rights_metadata FROM assets WHERE project_id=%s ORDER BY created_at",
                (project_id,),
            )
            assets = [
                {
                    "id": str(row[0]),
                    "asset_type": row[1],
                    "mime_type": row[2],
                    "size_bytes": row[3],
                    "rights_metadata": row[4],
                }
                for row in cursor.fetchall()
            ]
        request = PlanningRequest(
            project_id=str(project_id),
            project_type="MUSIC_VIDEO",
            project_title=project[2],
            creative_brief=project[0],
            target_duration_seconds=body.target_duration_seconds,
            aspect_ratio=project[1],
            visual_tone=body.visual_tone,
            narrative_approach=body.narrative_approach,
            performance_presence=body.performance_presence,
            pacing=body.pacing,
            constraints=body.constraints,
            reference_assets=assets,
            maximum_shot_count=min(body.maximum_shot_count, settings.planning_max_shot_count),
            correlation_id=body.idempotency_key,
            idempotency_key=body.idempotency_key,
        )
        cursor.execute(
            "INSERT INTO jobs(project_id,job_type,status,idempotency_key,max_retries,request_data) "
            "VALUES (%s,'PLAN_GENERATION','QUEUED',%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id,status",
            (project_id, body.idempotency_key, settings.max_retries, Jsonb(request.model_dump())),
        )
        created = cursor.fetchone()
        if created is None:
            cursor.execute(
                "SELECT id,status,project_id,job_type FROM jobs WHERE idempotency_key=%s",
                (body.idempotency_key,),
            )
            existing = cursor.fetchone()
            if existing[2] != project_id or existing[3] != "PLAN_GENERATION":
                raise HTTPException(409, "idempotency key belongs to another request")
            conn.commit()
            return {"job_id": str(existing[0]), "status": existing[1], "deduplicated": True}
        record_event(cursor, project_id, "PLAN_GENERATION_SUBMITTED", "job", created[0])
        conn.commit()
    enqueue(str(created[0]), "planning")
    return {"job_id": str(created[0]), "status": created[1], "deduplicated": False}


@app.get("/projects/{project_id}/plans", response_model=list[PlanSummary])
def list_plans(project_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM projects WHERE id=%s AND project_type='MUSIC_VIDEO'", (project_id,)
        )
        if cursor.fetchone() is None:
            raise HTTPException(404, "music video project not found")
        cursor.execute(
            "SELECT id,version,status,concept_title,provider,model,created_at,approved_at FROM project_plans WHERE project_id=%s ORDER BY version DESC",
            (project_id,),
        )
        rows = cursor.fetchall()
    return [
        dict(
            zip(
                (
                    "id",
                    "version",
                    "status",
                    "concept_title",
                    "provider",
                    "model",
                    "created_at",
                    "approved_at",
                ),
                row,
            )
        )
        for row in rows
    ]


@app.get("/projects/{project_id}/plans/{plan_id}", response_model=PlanDetail)
def get_plan(project_id: UUID, plan_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {_PLAN_COLUMNS} FROM project_plans WHERE id=%s AND project_id=%s",
            (plan_id, project_id),
        )
        row = cursor.fetchone()
        evidence = None
        if row and row[14]:
            cursor.execute(
                "SELECT payload->'evidence' FROM events WHERE entity_id=%s "
                "AND event_type='OPENAI_PLANNING_RESPONSE' ORDER BY created_at DESC LIMIT 1",
                (row[14],),
            )
            evidence_row = cursor.fetchone()
            evidence = evidence_row[0] if evidence_row else None
    if row is None:
        raise HTTPException(404, "plan not found")
    return _plan_detail(row, evidence)


@app.post(
    "/projects/{project_id}/plans/{plan_id}/versions", status_code=201, response_model=PlanDetail
)
def create_plan_version(project_id: UUID, plan_id: UUID, body: PlanVersionCreate):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {_PLAN_COLUMNS} FROM project_plans WHERE id=%s AND project_id=%s",
            (plan_id, project_id),
        )
        parent = cursor.fetchone()
        if parent is None:
            raise HTTPException(404, "plan not found")
        inputs = parent[5]
        cursor.execute("SELECT id FROM assets WHERE project_id=%s", (project_id,))
        allowed = {str(row[0]) for row in cursor.fetchall()}
        try:
            validate_plan(
                body,
                target_duration=float(inputs["target_duration_seconds"]),
                maximum_shots=int(inputs["maximum_shot_count"]),
                allowed_asset_ids=allowed,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        cursor.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,))
        cursor.execute(
            "SELECT COALESCE(max(version),0)+1 FROM project_plans WHERE project_id=%s",
            (project_id,),
        )
        version = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO project_plans(project_id,version,parent_plan_id,planning_inputs,concept_title,logline,treatment,creative_direction,shot_plan,provider,model,prompt_schema_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'producer','manual-edit',%s) RETURNING id",
            (
                project_id,
                version,
                plan_id,
                Jsonb(inputs),
                body.concept_title,
                body.logline,
                body.treatment,
                Jsonb(body.creative_direction.model_dump()),
                Jsonb([shot.model_dump() for shot in body.shots]),
                body.schema_version,
            ),
        )
        new_id = cursor.fetchone()[0]
        record_event(
            cursor,
            project_id,
            "PLAN_VERSION_CREATED",
            "project_plan",
            new_id,
            {"parent_plan_id": str(plan_id), "version": version},
        )
        conn.commit()
    return get_plan(project_id, new_id)


@app.post("/projects/{project_id}/plans/{plan_id}/approve", response_model=PlanApprovalResponse)
def approve_plan(project_id: UUID, plan_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {_PLAN_COLUMNS} FROM project_plans WHERE id=%s AND project_id=%s FOR UPDATE",
            (plan_id, project_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404, "plan not found")
        detail = _plan_detail(row)
        try:
            result = PlanningResult.model_validate(
                {
                    key: detail[key]
                    for key in (
                        "schema_version",
                        "concept_title",
                        "logline",
                        "treatment",
                        "creative_direction",
                        "shots",
                    )
                }
            )
        except ValidationError as exc:
            raise HTTPException(422, "stored plan is invalid") from exc
        cursor.execute("SELECT id FROM assets WHERE project_id=%s", (project_id,))
        allowed = {str(item[0]) for item in cursor.fetchall()}
        try:
            validate_plan(
                result,
                target_duration=float(detail["planning_inputs"]["target_duration_seconds"]),
                maximum_shots=int(detail["planning_inputs"]["maximum_shot_count"]),
                allowed_asset_ids=allowed,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        cursor.execute("SELECT id FROM shots WHERE source_plan_id=%s ORDER BY ordinal", (plan_id,))
        existing = [item[0] for item in cursor.fetchall()]
        if row[3] == "APPROVED" and row[17] is not None:
            conn.commit()
            return PlanApprovalResponse(
                plan_id=plan_id, status="APPROVED", created_shot_ids=existing, idempotent=True
            )
        cursor.execute(
            "SELECT id FROM project_plans WHERE project_id=%s AND status='APPROVED' AND id<>%s FOR UPDATE",
            (project_id, plan_id),
        )
        prior = cursor.fetchone()
        if prior:
            cursor.execute(
                "SELECT count(*) FROM generation_attempts ga JOIN shots s ON s.id=ga.shot_id WHERE s.source_plan_id=%s",
                (prior[0],),
            )
            if cursor.fetchone()[0]:
                raise HTTPException(
                    409, "approved plan cannot be replaced after generation has started"
                )
            cursor.execute(
                "SELECT s.source_plan_item_key,s.title,s.prompt,s.intended_duration,p.shot_plan "
                "FROM shots s JOIN project_plans p ON p.id=s.source_plan_id "
                "WHERE s.source_plan_id=%s ORDER BY s.ordinal",
                (prior[0],),
            )
            prior_rows = cursor.fetchall()
            expected = {item["item_key"]: item for item in (prior_rows[0][4] if prior_rows else [])}
            changed = len(prior_rows) != len(expected) or any(
                item_key not in expected
                or title != expected[item_key]["title"]
                or prompt != expected[item_key]["prompt"]
                or float(duration) != float(expected[item_key]["duration_seconds"])
                for item_key, title, prompt, duration, _shot_plan in prior_rows
            )
            if changed:
                raise HTTPException(
                    409, "approved plan cannot be replaced after its shots were changed"
                )
            cursor.execute("DELETE FROM shots WHERE source_plan_id=%s", (prior[0],))
            cursor.execute("UPDATE project_plans SET status='SUPERSEDED' WHERE id=%s", (prior[0],))
            record_event(
                cursor,
                project_id,
                "PLAN_SUPERSEDED",
                "project_plan",
                prior[0],
                {"replacement_plan_id": str(plan_id)},
            )
        cursor.execute(
            "SELECT COALESCE(max(ordinal),0) FROM shots WHERE project_id=%s AND source_plan_id IS NULL",
            (project_id,),
        )
        offset = cursor.fetchone()[0]
        created = []
        for shot in result.shots:
            cursor.execute(
                "INSERT INTO shots(project_id,ordinal,title,prompt,intended_duration,status,source_plan_id,source_plan_item_key) VALUES (%s,%s,%s,%s,%s,'PLANNED',%s,%s) ON CONFLICT (source_plan_id,source_plan_item_key) WHERE source_plan_id IS NOT NULL DO NOTHING RETURNING id",
                (
                    project_id,
                    offset + shot.ordinal,
                    shot.title,
                    shot.prompt,
                    shot.duration_seconds,
                    plan_id,
                    shot.item_key,
                ),
            )
            inserted = cursor.fetchone()
            if inserted:
                created.append(inserted[0])
        cursor.execute(
            "UPDATE project_plans SET status='APPROVED',approved_at=now(),materialized_at=now() WHERE id=%s",
            (plan_id,),
        )
        record_event(
            cursor, project_id, "PLAN_APPROVED", "project_plan", plan_id, {"version": row[2]}
        )
        record_event(
            cursor,
            project_id,
            "PLAN_SHOTS_MATERIALIZED",
            "project_plan",
            plan_id,
            {"shot_ids": [str(value) for value in created]},
        )
        conn.commit()
    return PlanApprovalResponse(
        plan_id=plan_id, status="APPROVED", created_shot_ids=created, idempotent=False
    )


@app.get("/projects/{project_id}/events")
def events(project_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT event_type,entity_type,entity_id,payload,created_at FROM events WHERE project_id=%s ORDER BY created_at",
            (project_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            "event_type": r[0],
            "entity_type": r[1],
            "entity_id": str(r[2]),
            "payload": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


@app.post("/projects/{project_id}/renders", status_code=202)
def create_render(project_id: UUID, body: RenderCreate):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT storage_key FROM assets WHERE id=%s AND project_id=%s AND asset_type='AUDIO'",
            (body.audio_asset_id, project_id),
        )
        audio = cursor.fetchone()
        if not audio:
            raise HTTPException(404, "audio asset not found")
        cursor.execute(
            "SELECT v.id,v.storage_key,v.duration FROM shot_variants v JOIN shots s ON s.id=v.shot_id WHERE v.id = ANY(%s) AND s.project_id=%s",
            ([str(value) for value in body.variant_ids], project_id),
        )
        variants = cursor.fetchall()
        if len(variants) != len(body.variant_ids):
            raise HTTPException(404, "one or more variants not found")
        keys = {str(row[0]): row[1] for row in variants}
        durations = {str(row[0]): float(row[2]) for row in variants}
        shot_duration = sum(durations[str(value)] for value in body.variant_ids)
        card_duration = sum(
            card.duration_seconds for card in (body.intro_card, body.outro_card) if card
        )
        expected_duration = shot_duration + card_duration
        audio_path = LocalStorage(settings.storage_root).path(audio[0])
        try:
            audio_duration = probe_duration(audio_path)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            raise HTTPException(422, "audio duration could not be read") from exc
        if not math.isfinite(body.audio_start_seconds):
            raise HTTPException(422, "audio start must be finite")
        if body.audio_start_seconds + expected_duration > audio_duration + 0.01:
            raise HTTPException(
                422,
                "audio does not have enough remaining duration for the selected video",
            )
        render_spec = {
            "variant_ids": [str(value) for value in body.variant_ids],
            "audio_asset_id": str(body.audio_asset_id),
            "audio_key": audio[0],
            "variant_keys": [keys[str(value)] for value in body.variant_ids],
            "audio_start_seconds": body.audio_start_seconds,
            "intro_card": body.intro_card.model_dump() if body.intro_card else None,
            "outro_card": body.outro_card.model_dump() if body.outro_card else None,
            "shot_duration": shot_duration,
            "card_duration": card_duration,
            "expected_duration": expected_duration,
            "audio_duration": audio_duration,
        }
        cursor.execute(
            "INSERT INTO renders(project_id,status,render_spec) VALUES (%s,'QUEUED',%s) RETURNING id",
            (project_id, Jsonb(render_spec)),
        )
        render_id = cursor.fetchone()[0]
        record_event(cursor, project_id, "RENDER_SUBMITTED", "render", render_id)
        conn.commit()
    enqueue(str(render_id), "render")
    return {"id": str(render_id), "status": "QUEUED"}


@app.get("/projects/{project_id}/renders")
def list_renders(project_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id,status,output_storage_key,mime_type,duration,error_data,render_spec,created_at,started_at,completed_at "
            "FROM renders WHERE project_id=%s ORDER BY created_at DESC",
            (project_id,),
        )
        rows = cursor.fetchall()
    fields = (
        "id",
        "status",
        "output_storage_key",
        "mime_type",
        "duration",
        "error_data",
        "render_spec",
        "created_at",
        "started_at",
        "completed_at",
    )
    return [dict(zip(fields, row)) for row in rows]


@app.get("/renders/{render_id}")
def get_render(render_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id,status,output_storage_key,mime_type,duration,error_data,render_spec,created_at,started_at,completed_at FROM renders WHERE id=%s",
            (render_id,),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(404, "render not found")
    return dict(
        zip(
            (
                "id",
                "status",
                "output_storage_key",
                "mime_type",
                "duration",
                "error_data",
                "render_spec",
                "created_at",
                "started_at",
                "completed_at",
            ),
            row,
        )
    )


@app.get("/renders/{render_id}/media")
def get_render_media(render_id: UUID, download: bool = Query(False)):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT output_storage_key,mime_type,status FROM renders WHERE id=%s", (render_id,)
        )
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, "render not found")
    if row[2] != "SUCCEEDED" or not row[0]:
        raise HTTPException(409, "render output is not available")
    return media_response(row[0], row[1] or "video/mp4", download)
