from datetime import UTC, datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from backend.app.db import connection
from backend.app.domain.models import JobStatus

ALLOWED = {
    JobStatus.QUEUED: {JobStatus.SUBMITTING, JobStatus.CANCEL_REQUESTED},
    JobStatus.SUBMITTING: {
        JobStatus.PROVIDER_PENDING,
        JobStatus.PROCESSING,
        JobStatus.FAILED,
        JobStatus.RETRY_SCHEDULED,
    },
    JobStatus.PROVIDER_PENDING: {
        JobStatus.PROCESSING,
        JobStatus.CANCEL_REQUESTED,
        JobStatus.TIMED_OUT,
        JobStatus.FAILED,
        JobStatus.RETRY_SCHEDULED,
    },
    JobStatus.PROCESSING: {
        JobStatus.DOWNLOADING,
        JobStatus.CANCEL_REQUESTED,
        JobStatus.FAILED,
        JobStatus.RETRY_SCHEDULED,
    },
    JobStatus.DOWNLOADING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.RETRY_SCHEDULED},
    JobStatus.RETRY_SCHEDULED: {JobStatus.SUBMITTING, JobStatus.FAILED},
    JobStatus.CANCEL_REQUESTED: {JobStatus.CANCELLED},
}


def transition(job_id: UUID, target: JobStatus, error: dict | None = None) -> None:
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT status, project_id FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("job not found")
        current = JobStatus(row[0])
        if target == current:
            return
        if target not in ALLOWED.get(current, set()):
            raise ValueError(f"invalid job transition {current} -> {target}")
        now = datetime.now(UTC)
        cursor.execute(
            "UPDATE jobs SET status=%s, error_data=%s, started_at=COALESCE(started_at, CASE WHEN %s='SUBMITTING' THEN %s ELSE started_at END), completed_at=CASE WHEN %s IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT') THEN %s ELSE completed_at END, updated_at=%s WHERE id=%s",
            (
                target,
                Jsonb(error) if error is not None else None,
                target,
                now,
                target,
                now,
                now,
                job_id,
            ),
        )
        cursor.execute(
            "INSERT INTO events (project_id,event_type,actor_type,entity_type,entity_id,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                row[1],
                "JOB_STATE_CHANGED",
                "worker",
                "job",
                job_id,
                Jsonb({"from": current, "to": target}),
            ),
        )
        conn.commit()
