from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.config import settings

pytestmark = pytest.mark.integration


def create_project(client, title: str = "Acceptance project") -> str:
    response = client.post(
        "/projects",
        json={"title": title, "creative_brief": "Integration evidence"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def create_shot(client, project_id: str, ordinal: int = 1, duration: float = 0.3) -> str:
    response = client.post(
        f"/projects/{project_id}/shots",
        json={
            "ordinal": ordinal,
            "title": f"Shot {ordinal}",
            "prompt": "A test frame",
            "intended_duration": duration,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def submit_generation(client, shot_id: str, key: str, reference_asset_id: str | None = None):
    params = {"idempotency_key": key}
    if reference_asset_id:
        params["reference_asset_id"] = reference_asset_id
    return client.post(f"/shots/{shot_id}/generations", params=params)


def test_project_upload_shot_job_lookup_events_and_validation(client, db, integration_environment):
    project_id = create_project(client)
    payload = b"valid-test-audio"
    rights = {"source": "generated", "cleared": True}
    upload = client.post(
        f"/projects/{project_id}/assets",
        files={"file": ("test.wav", payload, "audio/wav")},
        data={"asset_type": "AUDIO", "rights_metadata": json.dumps(rights)},
    )
    assert upload.status_code == 201
    asset = upload.json()
    assert asset["checksum"] == hashlib.sha256(payload).hexdigest()
    stored_path = Path(integration_environment["storage_root"]) / asset["storage_key"]
    assert stored_path.read_bytes() == payload
    with db() as conn:
        row = conn.execute(
            "SELECT mime_type,size_bytes,checksum,rights_metadata FROM assets WHERE id=%s",
            (asset["id"],),
        ).fetchone()
    assert row == ("audio/wav", len(payload), asset["checksum"], rights)

    shot_id = create_shot(client, project_id)
    submitted = submit_generation(client, shot_id, "api-coverage")
    assert submitted.status_code == 202
    job = client.get(f"/jobs/{submitted.json()['job_id']}")
    assert job.status_code == 200
    assert job.json()["status"] == "QUEUED"
    assert client.get(f"/shots/{shot_id}/variants").json() == []
    event_types = [
        event["event_type"] for event in client.get(f"/projects/{project_id}/events").json()
    ]
    assert event_types == ["PROJECT_CREATED", "ASSET_UPLOADED", "JOB_SUBMITTED"]

    missing_id = uuid4()
    assert client.get(f"/projects/{missing_id}").status_code == 404
    assert client.get(f"/jobs/{missing_id}").status_code == 404
    assert client.get(f"/renders/{missing_id}").status_code == 404
    assert submit_generation(client, str(missing_id), "missing").status_code == 404
    assert (
        client.post(
            f"/projects/{project_id}/shots",
            json={"ordinal": 0, "prompt": "bad", "intended_duration": 1},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/projects/{missing_id}/shots",
            json={"ordinal": 1, "prompt": "bad", "intended_duration": 1},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/projects/{project_id}/assets", files={"file": ("", b"", "audio/wav")}
        ).status_code
        == 422
    )
    unsafe = client.post(
        f"/projects/{project_id}/assets",
        files={"file": ("../../escape.wav", b"bad", "audio/wav")},
    )
    assert unsafe.status_code == 400
    assert not (Path(integration_environment["storage_root"]).parent / "escape.wav").exists()
    invalid_rights = client.post(
        f"/projects/{project_id}/assets",
        files={"file": ("bad.wav", b"bad", "audio/wav")},
        data={"rights_metadata": "not-json"},
    )
    assert invalid_rights.status_code == 422


def test_async_idempotent_generation_and_separate_worker(
    client, db, integration_environment, worker_process, wait_for
):
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=0.25)
    delay = 0.8
    settings.mock_provider_delay_seconds = delay

    started = time.monotonic()
    first = submit_generation(client, shot_id, "same-request")
    response_time = time.monotonic() - started
    duplicate = submit_generation(client, shot_id, "same-request")
    distinct = submit_generation(client, shot_id, "distinct-request")
    assert first.status_code == duplicate.status_code == distinct.status_code == 202
    assert response_time < delay / 2
    assert first.json()["job_id"] == duplicate.json()["job_id"]
    assert duplicate.json()["deduplicated"] is True
    assert distinct.json()["job_id"] != first.json()["job_id"]
    assert integration_environment["redis"].llen(integration_environment["queue_name"]) == 2
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2

    worker = worker_process(delay=delay)
    for job_id in (first.json()["job_id"], distinct.json()["job_id"]):
        wait_for(
            lambda job_id=job_id: client.get(f"/jobs/{job_id}").json(),
            lambda value: value["status"] == "SUCCEEDED",
            timeout=12,
        )
    variants = client.get(f"/shots/{shot_id}/variants").json()
    assert len(variants) == 2
    assert all(
        (Path(integration_environment["storage_root"]) / item["storage_key"]).exists()
        for item in variants
    )
    with db() as conn:
        attempts = conn.execute(
            "SELECT job_id,count(*) FROM generation_attempts GROUP BY job_id ORDER BY job_id"
        ).fetchall()
        assert len(attempts) == 2 and all(count == 1 for _, count in attempts)
        transitions = conn.execute(
            "SELECT payload->>'to' FROM events WHERE entity_id=%s AND event_type='JOB_STATE_CHANGED' ORDER BY created_at,id",
            (first.json()["job_id"],),
        ).fetchall()
    assert [row[0] for row in transitions] == [
        "SUBMITTING",
        "PROVIDER_PENDING",
        "PROCESSING",
        "DOWNLOADING",
        "SUCCEEDED",
    ]
    assert integration_environment["redis"].llen(integration_environment["queue_name"]) == 0
    print(f"async response={response_time:.3f}s mock_delay={delay:.3f}s worker_pid={worker.pid}")


def test_idempotency_key_conflict_is_useful(client):
    project_id = create_project(client)
    first_shot = create_shot(client, project_id, ordinal=1)
    second_shot = create_shot(client, project_id, ordinal=2)
    assert submit_generation(client, first_shot, "shared-key").status_code == 202
    conflict = submit_generation(client, second_shot, "shared-key")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency key belongs to another request"
