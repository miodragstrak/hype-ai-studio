from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.config import settings
from backend.app.queue import enqueue

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FAKE_SECRET = "sk-deliberate-integration-secret"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(tmp_path: Path, mode: str):
    port = free_port()
    count = tmp_path / f"{mode}-count.txt"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.tests.fake_openai_server",
            str(port),
            mode,
            str(count),
            FAKE_SECRET,
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.terminate()
        pytest.fail("fake OpenAI server did not start")
    return process, f"http://127.0.0.1:{port}/v1", count


def openai_worker(worker_process, base_url: str, **extra):
    environment = {
        "PLANNING_PROVIDER": "openai",
        "VIDEO_PROVIDER": "mock",
        "OPENAI_API_KEY": FAKE_SECRET,
        "OPENAI_BASE_URL": base_url,
        "OPENAI_PLANNING_MODEL": "gpt-5.6-sol",
        "OPENAI_PLANNING_TIMEOUT_SECONDS": "5",
        "OPENAI_PLANNING_MAX_OUTPUT_TOKENS": "12000",
        "OPENAI_PLANNING_SOFT_LIMIT_USD": "2",
        "OPENAI_PLANNING_HARD_LIMIT_USD": "5",
        **extra,
    }
    return worker_process(extra_env=environment)


def create_project(client, title="OpenAI planning integration") -> str:
    response = client.post(
        "/projects",
        json={
            "project_type": "MUSIC_VIDEO",
            "title": title,
            "creative_brief": "An imaginary silver performance progresses through practical light.",
            "aspect_ratio": "16:9",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def submit(client, project_id: str, key: str):
    return client.post(
        f"/projects/{project_id}/plans/generate",
        json={
            "target_duration_seconds": 10,
            "visual_tone": "silver nocturne",
            "narrative_approach": "escalating abstraction",
            "performance_presence": "one imaginary performer",
            "pacing": "measured",
            "constraints": "No text or logos.",
            "use_reference_assets": False,
            "maximum_shot_count": 2,
            "idempotency_key": key,
        },
    )


def wait_terminal(client, job_id: str, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            return job
        time.sleep(0.05)
    pytest.fail("OpenAI planning job did not reach a terminal state")


def test_openai_separate_worker_persists_usage_and_redelivery_without_resubmission(
    client, db, worker_process, tmp_path
):
    server, base_url, count = start_server(tmp_path, "success")
    try:
        settings.planning_provider = "openai"
        settings.openai_api_key = None
        project_id = create_project(client)
        worker = openai_worker(
            worker_process, base_url, OPENAI_PLANNING_SOFT_LIMIT_USD="0.1"
        )
        started = time.monotonic()
        response = submit(client, project_id, f"openai-success-{uuid4()}")
        assert response.status_code == 202
        assert time.monotonic() - started < 0.5
        job_id = response.json()["job_id"]
        assert wait_terminal(client, job_id)["status"] == "SUCCEEDED"
        assert count.read_text() == "1"
        plans = client.get(f"/projects/{project_id}/plans").json()
        assert len(plans) == 1
        assert plans[0]["provider"] == "openai"
        assert plans[0]["model"] == "gpt-5.6-sol"
        detail = client.get(f"/projects/{project_id}/plans/{plans[0]['id']}").json()
        assert detail["provider_evidence"]["provider_response_id"] == "resp_local_002b"
        assert detail["provider_evidence"]["usage"]["total_tokens"] == 360
        events = client.get(f"/projects/{project_id}/events").json()
        response_event = next(
            event for event in events if event["event_type"] == "OPENAI_PLANNING_RESPONSE"
        )
        assert any(
            event["event_type"] == "OPENAI_PLANNING_SOFT_LIMIT_REACHED"
            for event in events
        )
        evidence = response_event["payload"]["evidence"]
        assert evidence["provider_response_id"] == "resp_local_002b"
        assert evidence["usage"] == {
            "input_tokens": 120,
            "output_tokens": 240,
            "total_tokens": 360,
        }
        assert evidence["cost"]["estimated_total_cost_usd"] == pytest.approx(0.00528)
        assert evidence["cost"]["actual_cost_usd"] is None

        with db() as conn:
            conn.execute("DELETE FROM project_plans WHERE source_job_id=%s", (job_id,))
            conn.execute(
                "UPDATE jobs SET status='PROCESSING',completed_at=NULL WHERE id=%s", (job_id,)
            )
        enqueue(job_id, "planning")
        assert wait_terminal(client, job_id)["status"] == "SUCCEEDED"
        assert count.read_text() == "1"
        recovered = client.get(f"/projects/{project_id}/plans").json()
        assert len(recovered) == 1

        approval = client.post(f"/projects/{project_id}/plans/{recovered[0]['id']}/approve")
        assert approval.status_code == 200
        assert len(approval.json()["created_shot_ids"]) == 2
        with db() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM generation_attempts WHERE project_id=%s", (project_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM shot_variants v JOIN shots s ON s.id=v.shot_id "
                    "WHERE s.project_id=%s",
                    (project_id,),
                ).fetchone()[0]
                == 0
            )
        assert worker.poll() is None
    finally:
        server.terminate()
        server.wait(timeout=3)


def test_retryable_rate_limit_retries_once_then_succeeds(client, worker_process, tmp_path):
    server, base_url, count = start_server(tmp_path, "transient")
    try:
        settings.planning_provider = "openai"
        project_id = create_project(client, "Transient")
        openai_worker(worker_process, base_url)
        response = submit(client, project_id, f"openai-transient-{uuid4()}")
        job = wait_terminal(client, response.json()["job_id"])
        assert job["status"] == "SUCCEEDED"
        assert job["retry_count"] == 1
        assert count.read_text() == "2"
    finally:
        server.terminate()
        server.wait(timeout=3)


def test_hard_cap_blocks_before_sdk_submission(client, worker_process, tmp_path):
    server, base_url, count = start_server(tmp_path, "success")
    try:
        settings.planning_provider = "openai"
        project_id = create_project(client, "Budget cap")
        openai_worker(
            worker_process,
            base_url,
            OPENAI_PLANNING_SOFT_LIMIT_USD="0.005",
            OPENAI_PLANNING_HARD_LIMIT_USD="0.01",
        )
        response = submit(client, project_id, f"openai-cap-{uuid4()}")
        job = wait_terminal(client, response.json()["job_id"])
        assert job["status"] == "FAILED"
        assert job["error_data"]["code"] == "OPENAI_PLANNING_BUDGET_EXCEEDED"
        assert not count.exists()
    finally:
        server.terminate()
        server.wait(timeout=3)


def test_auth_failure_is_sanitized_everywhere(client, db, worker_process, tmp_path):
    server, base_url, count = start_server(tmp_path, "auth")
    try:
        settings.planning_provider = "openai"
        project_id = create_project(client, "Secret safety")
        openai_worker(worker_process, base_url)
        response = submit(client, project_id, f"openai-auth-{uuid4()}")
        job = wait_terminal(client, response.json()["job_id"])
        assert job["status"] == "FAILED"
        assert job["retry_count"] == 0
        assert count.read_text() == "1"
        events = client.get(f"/projects/{project_id}/events").json()
        with db() as conn:
            persisted = conn.execute(
                "SELECT request_data,error_data FROM jobs WHERE id=%s", (response.json()["job_id"],)
            ).fetchone()
        inspected = str({"job": job, "events": events, "persisted": persisted})
        assert FAKE_SECRET not in inspected
        assert "Bearer" not in inspected
        assert client.get(f"/projects/{project_id}/plans").json() == []
    finally:
        server.terminate()
        server.wait(timeout=3)
