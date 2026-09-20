from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from backend.app.config import settings
from backend.app.domain.models import (
    GenerationResult,
    GenerationStatus,
    NormalizedStatus,
    OutputReference,
)
from backend.app.worker import process_generation
from backend.tests.test_api_integration import create_project, create_shot, submit_generation

pytestmark = pytest.mark.integration


class FakeRunwayProvider:
    provider = "runway"
    model = "gen4.5"

    def __init__(self, output: Path, submit_error: Exception | None = None):
        self.output = output
        self.submit_error = submit_error
        self.submit_calls = 0
        self.status_calls = 0
        self.lock = Lock()

    def submit_generation(self, request):
        with self.lock:
            self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return f"task-{request.correlation_id}"

    def get_status(self, provider_job_id):
        self.status_calls += 1
        return GenerationStatus(provider_job_id, NormalizedStatus.SUCCEEDED)

    def get_result(self, provider_job_id):
        return GenerationResult(
            provider_job_id,
            NormalizedStatus.SUCCEEDED,
            [OutputReference(str(self.output))],
            {"model": self.model},
        )

    def cancel(self, provider_job_id):
        return GenerationStatus(provider_job_id, NormalizedStatus.CANCELLED)


class PollingFailureProvider(FakeRunwayProvider):
    def get_status(self, provider_job_id):
        self.status_calls += 1
        raise ConnectionError("poll failed after known task id")


def runway_settings(tmp_path):
    settings.video_provider = "runway"
    settings.runwayml_api_secret = SecretStr("integration-test-secret")
    settings.runway_video_duration_seconds = 5
    settings.runway_video_ratio = "1280:720"
    settings.runway_model_credits_per_second = 12
    settings.runway_credit_usd_rate = 0.01
    settings.runway_poll_interval_seconds = 0.001
    settings.runway_task_timeout_seconds = 1
    output = tmp_path / "provider.mp4"
    output.write_bytes(b"fake-video")
    return output


def test_soft_warning_cost_and_idempotent_variant(client, db, monkeypatch, tmp_path):
    output = runway_settings(tmp_path)
    settings.runway_soft_limit_usd = 0.50
    settings.runway_hard_limit_usd = 30
    provider = FakeRunwayProvider(output)
    monkeypatch.setattr("backend.app.worker.create_video_provider", lambda *_: provider)
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=5)
    job_id = submit_generation(client, shot_id, "runway-soft").json()["job_id"]

    process_generation(job_id)
    process_generation(job_id)

    with db() as conn:
        attempt = conn.execute(
            "SELECT provider_job_id,cost_metadata,status FROM generation_attempts WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        variants = conn.execute(
            "SELECT count(*) FROM shot_variants WHERE generation_attempt_id=(SELECT id FROM generation_attempts WHERE job_id=%s)",
            (job_id,),
        ).fetchone()[0]
        events = {
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM events WHERE project_id=%s", (project_id,)
            )
        }
    assert provider.submit_calls == 1
    assert attempt[0].startswith("task-")
    assert attempt[1]["estimated_credits"] == 60
    assert attempt[1]["estimated_usd"] == 0.6
    assert attempt[1]["actual_usd"] is None
    assert attempt[2] == "SUCCEEDED"
    assert variants == 1
    assert "RUNWAY_SOFT_LIMIT_WARNING" in events


def test_hard_limit_rejects_before_provider_call(client, db, monkeypatch, tmp_path):
    runway_settings(tmp_path)
    settings.runway_soft_limit_usd = 0.25
    settings.runway_hard_limit_usd = 0.59
    called = False

    def forbidden_provider(*args):
        nonlocal called
        called = True
        raise AssertionError("provider must not be created")

    monkeypatch.setattr("backend.app.worker.create_video_provider", forbidden_provider)
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=5)
    job_id = submit_generation(client, shot_id, "runway-hard").json()["job_id"]
    process_generation(job_id)
    assert called is False
    assert (
        client.get(f"/jobs/{job_id}").json()["error_data"]["code"] == "RUNWAY_HARD_LIMIT_EXCEEDED"
    )
    events = client.get(f"/projects/{project_id}/events").json()
    assert any(event["event_type"] == "RUNWAY_HARD_LIMIT_REJECTED" for event in events)


def test_concurrent_reservations_cannot_exceed_cap(client, db, monkeypatch, tmp_path):
    output = runway_settings(tmp_path)
    settings.runway_soft_limit_usd = 0.59
    settings.runway_hard_limit_usd = 0.60
    provider = FakeRunwayProvider(output)
    monkeypatch.setattr("backend.app.worker.create_video_provider", lambda *_: provider)
    project_id = create_project(client)
    first = create_shot(client, project_id, ordinal=1, duration=5)
    second = create_shot(client, project_id, ordinal=2, duration=5)
    jobs = [
        submit_generation(client, first, "concurrent-1").json()["job_id"],
        submit_generation(client, second, "concurrent-2").json()["job_id"],
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(process_generation, jobs))
    with db() as conn:
        statuses = [
            row[0]
            for row in conn.execute("SELECT status FROM generation_attempts ORDER BY started_at")
        ]
        reserved = conn.execute(
            "SELECT COALESCE(sum((cost_metadata->>'estimated_usd')::numeric),0) "
            "FROM generation_attempts WHERE cost_metadata->>'budget_status'='reserved'"
        ).fetchone()[0]
    assert sorted(statuses) == ["REJECTED", "SUCCEEDED"]
    assert float(reserved) == 0.6
    assert provider.submit_calls == 1


def insert_known_attempt(db, job_id, project_id, shot_id, provider_job_id):
    with db() as conn:
        conn.execute(
            "INSERT INTO generation_attempts "
            "(project_id,shot_id,job_id,provider,model,provider_job_id,submitted_prompt,attempt_number,status,cost_metadata) "
            "VALUES (%s,%s,%s,'runway','gen4.5',%s,'prompt',1,'SUBMITTED',%s)",
            (
                project_id,
                shot_id,
                job_id,
                provider_job_id,
                Jsonb({"estimated_usd": 0.6, "budget_status": "reserved"}),
            ),
        )


def test_known_task_redelivery_resumes_without_submit(client, db, monkeypatch, tmp_path):
    output = runway_settings(tmp_path)
    provider = FakeRunwayProvider(output)
    monkeypatch.setattr("backend.app.worker.create_video_provider", lambda *_: provider)
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=5)
    job_id = submit_generation(client, shot_id, "known-task").json()["job_id"]
    insert_known_attempt(db, job_id, project_id, shot_id, "known-provider-id")

    process_generation(job_id)
    process_generation(job_id)

    assert provider.submit_calls == 0
    assert provider.status_calls == 1
    assert len(client.get(f"/shots/{shot_id}/variants").json()) == 1


def test_polling_failure_with_known_id_never_resubmits(client, db, monkeypatch, tmp_path):
    output = runway_settings(tmp_path)
    provider = PollingFailureProvider(output)
    monkeypatch.setattr("backend.app.worker.create_video_provider", lambda *_: provider)
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=5)
    job_id = submit_generation(client, shot_id, "known-poll-failure").json()["job_id"]
    insert_known_attempt(db, job_id, project_id, shot_id, "known-provider-id")

    process_generation(job_id)
    process_generation(job_id)

    assert provider.submit_calls == 0
    assert provider.status_calls == 1
    assert client.get(f"/jobs/{job_id}").json()["status"] == "FAILED"


def test_reserved_redelivery_and_ambiguous_error_are_sanitized(client, db, monkeypatch, tmp_path):
    output = runway_settings(tmp_path)
    provider = FakeRunwayProvider(output)
    monkeypatch.setattr("backend.app.worker.create_video_provider", lambda *_: provider)
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=5)
    job_id = submit_generation(client, shot_id, "reserved-task").json()["job_id"]
    insert_known_attempt(db, job_id, project_id, shot_id, None)

    process_generation(job_id)

    job = client.get(f"/jobs/{job_id}").json()
    assert provider.submit_calls == 0
    assert job["error_data"]["code"] == "AMBIGUOUS_PROVIDER_SUBMISSION"
    serialized = str(job) + str(client.get(f"/projects/{project_id}/events").json())
    assert "integration-test-secret" not in serialized


def test_submit_exception_never_retries_or_persists_secret(
    client, db, monkeypatch, tmp_path, caplog
):
    output = runway_settings(tmp_path)
    leaked = "integration-test-secret Bearer authorization-value"
    provider = FakeRunwayProvider(output, ConnectionResetError(leaked))
    monkeypatch.setattr("backend.app.worker.create_video_provider", lambda *_: provider)
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=5)
    job_id = submit_generation(client, shot_id, "ambiguous-task").json()["job_id"]

    process_generation(job_id)
    process_generation(job_id)

    job = client.get(f"/jobs/{job_id}").json()
    with db() as conn:
        persisted = conn.execute(
            "SELECT error_data,provider_metadata FROM generation_attempts WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    all_evidence = str(job) + str(persisted) + caplog.text
    assert provider.submit_calls == 1
    assert job["error_data"]["code"] == "AMBIGUOUS_PROVIDER_SUBMISSION"
    assert "integration-test-secret" not in all_evidence
    assert "authorization-value" not in all_evidence
