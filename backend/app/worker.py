import time
from pathlib import Path

from psycopg.types.json import Jsonb

from backend.app.config import settings
from backend.app.db import connection
from backend.app.domain.models import GenerationRequest, JobStatus, NormalizedStatus
from backend.app.providers.mock import MockVideoProvider
from backend.app.queue import dequeue, enqueue
from backend.app.render import compose_video
from backend.app.services.jobs import transition
from backend.app.storage.local import LocalStorage


def process_generation(job_id: str) -> None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, project_id, shot_id, retry_count, max_retries FROM jobs WHERE id=%s",
            (job_id,),
        )
        job = cursor.fetchone()
        if not job:
            return
        cursor.execute("SELECT prompt, intended_duration FROM shots WHERE id=%s", (job[2],))
        shot = cursor.fetchone()
    failure_mode = settings.mock_provider_failure_mode
    if failure_mode.startswith("transient:"):
        failure_count = int(failure_mode.partition(":")[2])
        failure_mode = "transient" if job[3] < failure_count else "none"
    provider = MockVideoProvider(
        settings.storage_root / "mock-provider",
        settings.mock_provider_delay_seconds,
        failure_mode,
    )
    attempt_id = None
    try:
        transition(job[0], JobStatus.SUBMITTING)
        request = GenerationRequest(shot[0], "16:9", float(shot[1]), correlation_id=str(job[0]))
        provider_id = provider.submit_generation(request)
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO generation_attempts (project_id,shot_id,job_id,provider,model,provider_job_id,submitted_prompt,parameters,attempt_number,status,started_at) VALUES (%s,%s,%s,'mock','mock-video-v1',%s,%s,%s,%s,'SUBMITTED',now()) RETURNING id",
                (job[1], job[2], job[0], provider_id, shot[0], Jsonb({}), job[3] + 1),
            )
            attempt_id = cursor.fetchone()[0]
            conn.commit()
        transition(job[0], JobStatus.PROVIDER_PENDING)
        transition(job[0], JobStatus.PROCESSING)
        while provider.get_status(provider_id).status == NormalizedStatus.PENDING:
            time.sleep(0.01)
        result = provider.get_result(provider_id)
        if result.status != NormalizedStatus.SUCCEEDED:
            provider_error = result.error
            message = provider_error.message if provider_error else "provider failed"
            exception = RuntimeError(message)
            exception.retryable = bool(provider_error and provider_error.retryable)  # type: ignore[attr-defined]
            raise exception
        transition(job[0], JobStatus.DOWNLOADING)
        storage = LocalStorage(settings.storage_root)
        key = f"projects/{job[1]}/variants/{job[0]}.mp4"
        storage.save_file(key, Path(result.outputs[0].uri))
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE generation_attempts SET status='SUCCEEDED',completed_at=now(),duration=%s,provider_metadata=%s WHERE id=%s",
                (shot[1], Jsonb(result.provider_metadata), attempt_id),
            )
            cursor.execute(
                "INSERT INTO shot_variants (shot_id,generation_attempt_id,storage_key,mime_type,duration,review_status) VALUES (%s,%s,%s,'video/mp4',%s,'UNREVIEWED') RETURNING id",
                (job[2], attempt_id, key, shot[1]),
            )
            variant_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) VALUES (%s,'VARIANT_CREATED','worker','shot_variant',%s,%s)",
                (job[1], variant_id, Jsonb({"job_id": str(job[0])})),
            )
            conn.commit()
        transition(job[0], JobStatus.SUCCEEDED)
    except Exception as exc:  # noqa: BLE001
        retryable = bool(getattr(exc, "retryable", True))
        error = {"message": str(exc), "retryable": retryable, "type": type(exc).__name__}
        if attempt_id is not None:
            with connection() as conn, conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE generation_attempts SET status='FAILED',completed_at=now(),error_data=%s WHERE id=%s",
                    (Jsonb(error), attempt_id),
                )
                conn.commit()
        if retryable and job[3] < job[4]:
            with connection() as conn, conn.cursor() as cursor:
                cursor.execute("UPDATE jobs SET retry_count=retry_count+1 WHERE id=%s", (job[0],))
                conn.commit()
            transition(job[0], JobStatus.RETRY_SCHEDULED, error)
            enqueue(str(job[0]))
        else:
            transition(job[0], JobStatus.FAILED, error)


def process_render(render_id: str) -> None:
    storage = LocalStorage(settings.storage_root)
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT project_id,render_spec FROM renders WHERE id=%s", (render_id,))
        row = cursor.fetchone()
        if not row:
            return
        cursor.execute(
            "UPDATE renders SET status='PROCESSING',started_at=now() WHERE id=%s", (render_id,)
        )
        conn.commit()
    try:
        spec = row[1]
        inputs = [storage.path(key) for key in spec["variant_keys"]]
        output_key = f"projects/{row[0]}/renders/{render_id}.mp4"
        duration = compose_video(inputs, storage.path(spec["audio_key"]), storage.path(output_key))
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE renders SET status='SUCCEEDED',output_storage_key=%s,mime_type='video/mp4',duration=%s,completed_at=now() WHERE id=%s",
                (output_key, duration, render_id),
            )
            cursor.execute(
                "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) VALUES (%s,'RENDER_COMPLETED','worker','render',%s,%s)",
                (row[0], render_id, Jsonb({"storage_key": output_key})),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE renders SET status='FAILED',error_data=%s,completed_at=now() WHERE id=%s",
                (Jsonb({"message": str(exc)}), render_id),
            )
            conn.commit()


def run() -> None:
    while True:
        item = dequeue()
        if item is None:
            continue
        if item["kind"] == "generation":
            process_generation(item["job_id"])
        elif item["kind"] == "render":
            process_render(item["job_id"])


if __name__ == "__main__":
    run()
