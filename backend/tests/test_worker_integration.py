from __future__ import annotations

import json
import math
import struct
import subprocess
import time
import wave
from itertools import pairwise
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.tests.test_api_integration import create_project, create_shot, submit_generation

pytestmark = pytest.mark.integration


def job_events(db, job_id: str) -> list[str]:
    with db() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT payload->>'to' FROM events WHERE entity_id=%s AND event_type='JOB_STATE_CHANGED' ORDER BY created_at,id",
                (job_id,),
            ).fetchall()
        ]


@pytest.mark.parametrize(
    ("failure_mode", "expected_status", "expected_retries", "expected_attempts"),
    [
        ("transient:1", "SUCCEEDED", 1, 2),
        ("permanent", "FAILED", 0, 1),
        ("transient:99", "FAILED", 2, 3),
    ],
)
def test_retry_and_failure_lifecycle(
    client,
    db,
    integration_environment,
    worker_process,
    wait_for,
    failure_mode,
    expected_status,
    expected_retries,
    expected_attempts,
):
    project_id = create_project(client, failure_mode)
    shot_id = create_shot(client, project_id)
    response = submit_generation(client, shot_id, failure_mode)
    job_id = response.json()["job_id"]
    settings.max_retries = 2
    worker_process(failure_mode=failure_mode, delay=0)
    final = wait_for(
        lambda: client.get(f"/jobs/{job_id}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=10,
    )
    assert final["status"] == expected_status
    assert final["retry_count"] == expected_retries
    with db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM generation_attempts WHERE job_id=%s", (job_id,)
            ).fetchone()[0]
            == expected_attempts
        )
    assert integration_environment["redis"].llen(integration_environment["queue_name"]) == 0
    transitions = job_events(db, job_id)
    assert transitions.count("RETRY_SCHEDULED") == expected_retries
    assert transitions[-1] == expected_status
    if expected_status == "FAILED":
        assert final["error_data"]["message"]
        assert "type" in final["error_data"]
        events = client.get(f"/projects/{project_id}/events").json()
        assert any(
            event["event_type"] == "JOB_STATE_CHANGED" and event["payload"]["to"] == "FAILED"
            for event in events
        )


def make_wav(path: Path, duration: float = 1.0, sample_rate: int = 8000) -> None:
    frames = bytearray()
    for index in range(int(duration * sample_rate)):
        sample = int(5000 * math.sin(2 * math.pi * 440 * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def make_split_wav(path: Path, duration: float = 2.0, sample_rate: int = 8000) -> None:
    frames = bytearray()
    for index in range(int(duration * sample_rate)):
        frequency = 220 if index < sample_rate else 880
        sample = int(5000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def test_variant_selection_and_async_render_through_worker(
    client, db, integration_environment, worker_process, wait_for, tmp_path
):
    project_id = create_project(client, "Render project")
    first_shot_id = create_shot(client, project_id, duration=0.4)
    second_shot_id = create_shot(client, project_id, ordinal=2, duration=0.4)
    generation_jobs = [
        submit_generation(client, first_shot_id, f"render-variant-{index}").json()["job_id"]
        for index in range(2)
    ]
    generation_jobs.append(
        submit_generation(client, second_shot_id, "render-second-shot").json()["job_id"]
    )
    generation_worker = worker_process(delay=0.05)
    for job_id in generation_jobs:
        wait_for(
            lambda job_id=job_id: client.get(f"/jobs/{job_id}").json(),
            lambda value: value["status"] == "SUCCEEDED",
            timeout=10,
        )
    generation_worker.terminate()
    generation_worker.wait(timeout=3)
    variants = client.get(f"/shots/{first_shot_id}/variants").json()
    second_variants = client.get(f"/shots/{second_shot_id}/variants").json()
    assert len(variants) == 2
    assert len(second_variants) == 1
    assert all(
        item["provider"] == "mock"
        and item["model"] == "mock-video-v1"
        and item["generation_mode"] == "text-to-video"
        for item in variants + second_variants
    )
    with db() as conn:
        attempt_job_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT ga.job_id FROM shot_variants sv JOIN generation_attempts ga ON ga.id=sv.generation_attempt_id WHERE sv.shot_id=%s",
                (first_shot_id,),
            ).fetchall()
        }
    assert attempt_job_ids == set(generation_jobs[:2])

    first_selection = client.post(
        f"/shots/{first_shot_id}/variants/{variants[0]['id']}/select"
    )
    other_shot_selection = client.post(
        f"/shots/{second_shot_id}/variants/{second_variants[0]['id']}/select"
    )
    replacement = client.post(
        f"/shots/{first_shot_id}/variants/{variants[1]['id']}/select"
    )
    assert first_selection.status_code == other_shot_selection.status_code == 200
    assert replacement.status_code == 200
    selected_variants = client.get(f"/shots/{first_shot_id}/variants").json()
    statuses = {item["id"]: item["review_status"] for item in selected_variants}
    assert statuses[variants[0]["id"]] == "SUPERSEDED"
    assert statuses[variants[1]["id"]] == "SELECTED"
    assert list(statuses.values()).count("SELECTED") == 1
    shots = client.get(f"/projects/{project_id}/shots").json()
    assert shots[0]["selected_variant_id"] == variants[1]["id"]
    assert shots[1]["selected_variant_id"] == second_variants[0]["id"]

    audio_path = tmp_path / "soundtrack.wav"
    make_split_wav(audio_path)
    upload = client.post(
        f"/projects/{project_id}/assets",
        files={"file": (audio_path.name, audio_path.read_bytes(), "audio/wav")},
        data={"rights_metadata": json.dumps({"generated_for_test": True})},
    )
    assert upload.status_code == 201
    invalid_start = client.post(
        f"/projects/{project_id}/renders",
        json={
            "variant_ids": [variants[1]["id"], second_variants[0]["id"]],
            "audio_asset_id": upload.json()["id"],
            "audio_start_seconds": -1,
        },
    )
    assert invalid_start.status_code == 422
    too_late = client.post(
        f"/projects/{project_id}/renders",
        json={
            "variant_ids": [variants[1]["id"], second_variants[0]["id"]],
            "audio_asset_id": upload.json()["id"],
            "audio_start_seconds": 1.3,
        },
    )
    assert too_late.status_code == 422
    assert "enough remaining duration" in too_late.json()["detail"]
    started = time.monotonic()
    render_response = client.post(
        f"/projects/{project_id}/renders",
        json={
            "variant_ids": [variants[1]["id"], second_variants[0]["id"]],
            "audio_asset_id": upload.json()["id"],
            "audio_start_seconds": 1,
        },
    )
    render_response_time = time.monotonic() - started
    assert render_response.status_code == 202
    assert render_response_time < 0.5
    render_id = render_response.json()["id"]
    assert client.get(f"/renders/{render_id}").json()["status"] == "QUEUED"

    render_worker = worker_process()
    render = wait_for(
        lambda: client.get(f"/renders/{render_id}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=12,
    )
    assert render_worker.poll() is None
    assert render["status"] == "SUCCEEDED", render["error_data"]
    output = Path(integration_environment["storage_root"]) / render["output_storage_key"]
    assert output.exists()
    listed_renders = client.get(f"/projects/{project_id}/renders")
    assert listed_renders.status_code == 200
    assert listed_renders.json()[0]["id"] == render_id
    preview = client.get(f"/renders/{render_id}/media")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "video/mp4"
    download = client.get(f"/renders/{render_id}/media", params={"download": "true"})
    assert "attachment" in download.headers["content-disposition"]
    probe = subprocess.run(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    metadata = json.loads(probe.stdout)
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in metadata["streams"]}
    assert codecs["video"] == "h264"
    assert codecs["audio"] == "aac"
    assert "mp4" in metadata["format"]["format_name"].split(",")
    duration = float(metadata["format"]["duration"])
    assert duration == pytest.approx(0.8, abs=0.25)
    assert float(render["duration"]) == pytest.approx(duration, abs=0.01)
    assert render["render_spec"]["audio_start_seconds"] == 1
    assert render["render_spec"]["expected_duration"] == pytest.approx(0.8)
    decoded = subprocess.run(
        [
            settings.ffmpeg_executable,
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-t",
            "0.25",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=5,
    ).stdout
    samples = struct.unpack(f"<{len(decoded) // 2}h", decoded)
    crossings = sum(
        1 for left, right in pairwise(samples) if (left < 0 <= right) or (left >= 0 > right)
    )
    estimated_frequency = crossings / (2 * (len(samples) / 8000))
    assert estimated_frequency == pytest.approx(880, abs=100)
    events = client.get(f"/projects/{project_id}/events").json()
    event_types = [event["event_type"] for event in events]
    assert event_types.count("VARIANT_CREATED") == 3
    assert event_types.count("VARIANT_SELECTED") == 3
    assert "RENDER_SUBMITTED" in event_types
    assert "RENDER_COMPLETED" in event_types
    print(
        f"render response={render_response_time:.3f}s output={output} "
        f"duration={duration:.3f}s codecs={codecs}"
    )

    render_worker.terminate()
    render_worker.wait(timeout=3)
    legacy = client.post(
        f"/projects/{project_id}/renders",
        json={
            "variant_ids": [variants[1]["id"], second_variants[0]["id"]],
            "audio_asset_id": upload.json()["id"],
        },
    )
    assert legacy.status_code == 202
    assert client.get(f"/renders/{legacy.json()['id']}").json()["render_spec"]["audio_start_seconds"] == 0
