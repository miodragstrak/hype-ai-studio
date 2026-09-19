import hashlib
import json
from pathlib import PurePath
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from backend.app.config import settings
from backend.app.db import connection
from backend.app.domain.models import JobStatus
from backend.app.queue import enqueue
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


class RenderCreate(BaseModel):
    variant_ids: list[UUID] = Field(min_length=1)
    audio_asset_id: UUID


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
    data = file.file.read()
    if not data:
        raise HTTPException(400, "uploaded file is empty")
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
            "latest_job_id": str(row[7]) if row[7] else None,
            "latest_job_status": row[8],
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
def submit_generation(shot_id: UUID, idempotency_key: str):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id,project_id FROM shots WHERE id=%s", (shot_id,))
        shot = cursor.fetchone()
        if not shot:
            raise HTTPException(404, "shot not found")
        cursor.execute(
            "INSERT INTO jobs(project_id,shot_id,job_type,status,idempotency_key,max_retries) VALUES (%s,%s,'VIDEO_GENERATION','QUEUED',%s,%s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id,status",
            (shot[1], shot_id, idempotency_key, settings.max_retries),
        )
        created = cursor.fetchone()
        if created is None:
            cursor.execute(
                "SELECT id,status,shot_id FROM jobs WHERE idempotency_key=%s", (idempotency_key,)
            )
            existing = cursor.fetchone()
            if existing[2] != shot_id:
                raise HTTPException(409, "idempotency key belongs to another request")
            conn.commit()
            return {"job_id": str(existing[0]), "status": existing[1], "deduplicated": True}
        job_id = created[0]
        record_event(
            cursor, shot[1], "JOB_SUBMITTED", "job", job_id, {"job_type": "VIDEO_GENERATION"}
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
            "a.attempt_number,a.job_id FROM shot_variants v JOIN generation_attempts a ON a.id=v.generation_attempt_id "
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
            "SELECT storage_key FROM assets WHERE id=%s AND project_id=%s",
            (body.audio_asset_id, project_id),
        )
        audio = cursor.fetchone()
        if not audio:
            raise HTTPException(404, "audio asset not found")
        cursor.execute(
            "SELECT v.id,v.storage_key FROM shot_variants v JOIN shots s ON s.id=v.shot_id WHERE v.id = ANY(%s) AND s.project_id=%s",
            ([str(value) for value in body.variant_ids], project_id),
        )
        variants = cursor.fetchall()
        if len(variants) != len(body.variant_ids):
            raise HTTPException(404, "one or more variants not found")
        keys = {str(row[0]): row[1] for row in variants}
        render_spec = {
            "variant_ids": [str(value) for value in body.variant_ids],
            "audio_asset_id": str(body.audio_asset_id),
            "audio_key": audio[0],
            "variant_keys": [keys[str(value)] for value in body.variant_ids],
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
            "SELECT id,status,output_storage_key,mime_type,duration,error_data,created_at,started_at,completed_at "
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
        "created_at",
        "started_at",
        "completed_at",
    )
    return [dict(zip(fields, row)) for row in rows]


@app.get("/renders/{render_id}")
def get_render(render_id: UUID):
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id,status,output_storage_key,mime_type,duration,error_data,created_at,started_at,completed_at FROM renders WHERE id=%s",
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
