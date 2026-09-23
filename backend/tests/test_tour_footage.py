from __future__ import annotations

import json
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from backend.app.config import settings
from backend.tests.test_tour_render import estimate_frequency, make_video, seed_ready_tour

RIGHTS = {
    "source": "producer camera original",
    "license": "producer-owned",
    "usage_confirmed": True,
    "depicts_real_person": False,
    "likeness_consent_confirmed": False,
}


def upload(client, shot_id: str, path: Path, key: str, rights: dict = RIGHTS):
    return client.post(
        f"/shots/{shot_id}/footage",
        files={"file": (path.name, path.read_bytes(), "video/mp4")},
        data={"idempotency_key": key, "rights_metadata": json.dumps(rights)},
    )


def make_landscape_video(path: Path, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            settings.ffmpeg_executable,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=orange:s=320x180:r=25:d={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.integration
def test_tour_footage_validation_deduplication_and_normalization(
    client, db, worker_process, wait_for, integration_environment
):
    root = Path(integration_environment["storage_root"])
    _project_id, shot_ids = seed_ready_tour(db, root)
    shot_id = shot_ids[0]
    fixtures = root / "fixture-footage"
    valid = fixtures / "producer.mp4"
    short = fixtures / "short.mp4"
    make_video(valid, "green", 1.3)
    make_video(short, "yellow", 0.4)

    assert upload(client, shot_id, valid, "missing-rights", {**RIGHTS, "usage_confirmed": False}).status_code == 422
    assert upload(client, shot_id, short, "short").status_code == 422
    invalid = fixtures / "invalid.mp4"
    invalid.write_text("not media")
    assert upload(client, shot_id, invalid, "invalid").status_code == 422
    assert not any((root / "footage-validation").iterdir())

    submitted = upload(client, shot_id, valid, "same-upload")
    assert submitted.status_code == 202
    duplicate = upload(client, shot_id, valid, "same-upload")
    assert duplicate.status_code == 202
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["asset_id"] == submitted.json()["asset_id"]
    with db() as conn:
        assert conn.execute(
            "SELECT count(*) FROM assets WHERE shot_id=%s AND asset_type='TOUR_FOOTAGE'",
            (shot_id,),
        ).fetchone()[0] == 1

    worker_process()
    job = wait_for(
        lambda: client.get(f"/jobs/{submitted.json()['job_id']}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=20,
    )
    assert job["status"] == "SUCCEEDED", job["error_data"]
    variants = client.get(f"/shots/{shot_id}/variants").json()
    imported = next(item for item in variants if item["provider"] == "producer_upload")
    assert imported["model"] == "local-ffmpeg"
    assert imported["generation_mode"] == "producer-upload"
    assert imported["duration"] == pytest.approx(1, abs=0.12)
    assert imported["provenance"]["original_asset_id"] == submitted.json()["asset_id"]
    assert imported["provenance"]["normalization"]["padding_applied"] is False
    assert len([item for item in variants if item["provider"] == "producer_upload"]) == 1
    assert client.post(f"/shots/{shot_id}/variants/{imported['id']}/select").status_code == 200

    landscape = fixtures / "landscape.mp4"
    make_landscape_video(landscape, 1.4)
    replacement = upload(client, shot_id, landscape, "replacement")
    assert replacement.status_code == 202
    replacement_job = wait_for(
        lambda: client.get(f"/jobs/{replacement.json()['job_id']}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=20,
    )
    assert replacement_job["status"] == "SUCCEEDED", replacement_job["error_data"]
    variants = client.get(f"/shots/{shot_id}/variants").json()
    replacement_variant = next(
        item
        for item in variants
        if item["provider"] == "producer_upload" and item["id"] != imported["id"]
    )
    assert replacement_variant["provenance"]["normalization"]["padding_applied"] is True
    assert client.post(
        f"/shots/{shot_id}/variants/{replacement_variant['id']}/select"
    ).status_code == 200
    variants = client.get(f"/shots/{shot_id}/variants").json()
    assert next(item for item in variants if item["id"] == imported["id"])["review_status"] == "SUPERSEDED"
    assert next(item for item in variants if item["id"] == replacement_variant["id"])["review_status"] == "SELECTED"
    assert client.post(
        f"/shots/{shot_id}/variants/{replacement_variant['id']}/reject"
    ).status_code == 200
    shot = next(item for item in client.get(f"/projects/{_project_id}/shots").json() if item["id"] == shot_id)
    assert shot["selected_variant_id"] is None


@pytest.mark.integration
def test_tour_footage_stale_and_failed_normalization_leave_no_variant(
    client, db, worker_process, wait_for, integration_environment
):
    root = Path(integration_environment["storage_root"])
    project_id, shot_ids = seed_ready_tour(db, root)
    source = root / "fixture-footage/source.mp4"
    make_video(source, "purple", 1.3)
    submitted = upload(client, shot_ids[0], source, "stale-upload")
    assert submitted.status_code == 202
    with db() as conn:
        conn.execute("UPDATE project_plans SET status='SUPERSEDED' WHERE project_id=%s", (project_id,))
    stale_worker = worker_process()
    failed = wait_for(
        lambda: client.get(f"/jobs/{submitted.json()['job_id']}").json(),
        lambda value: value["status"] == "FAILED",
        timeout=10,
    )
    assert "provenance" in failed["error_data"]["message"]
    with db() as conn:
        assert conn.execute(
            "SELECT count(*) FROM shot_variants v JOIN generation_attempts a ON a.id=v.generation_attempt_id WHERE a.job_id=%s",
            (submitted.json()["job_id"],),
        ).fetchone()[0] == 0
    stale_worker.terminate()
    stale_worker.wait(timeout=3)

    with db() as conn:
        conn.execute("UPDATE project_plans SET status='APPROVED' WHERE project_id=%s", (project_id,))
    second = upload(client, shot_ids[0], source, "ffmpeg-failure")
    assert second.status_code == 202
    worker_process(extra_env={"FFMPEG_EXECUTABLE": "/missing/ffmpeg"})
    failed = wait_for(
        lambda: client.get(f"/jobs/{second.json()['job_id']}").json(),
        lambda value: value["status"] == "FAILED",
        timeout=10,
    )
    assert failed["error_data"]
    with db() as conn:
        assert conn.execute(
            "SELECT count(*) FROM shot_variants v JOIN generation_attempts a ON a.id=v.generation_attempt_id WHERE a.job_id=%s",
            (second.json()["job_id"],),
        ).fetchone()[0] == 0


@pytest.mark.integration
def test_five_producer_clips_render_to_40_second_tour(
    client, db, worker_process, wait_for, integration_environment
):
    root = Path(integration_environment["storage_root"])
    scenes = (("red", 330.0), ("green", 440.0), ("blue", 550.0), ("yellow", 660.0), ("purple", 770.0))
    project_id, shot_ids = seed_ready_tour(db, root, scenes=scenes, scene_duration=8, voice_duration=1)
    with db() as conn:
        conn.execute("UPDATE shots SET selected_variant_id=NULL WHERE project_id=%s", (project_id,))
    worker_process()
    imported_ids = []
    for index, shot_id in enumerate(shot_ids):
        source = root / f"fixture-footage/belgrade-{index + 1}.mp4"
        make_video(source, scenes[index][0], 8.3)
        response = upload(client, shot_id, source, f"belgrade-{index + 1}")
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        job = wait_for(
            lambda job_id=job_id: client.get(f"/jobs/{job_id}").json(),
            lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
            timeout=30,
        )
        assert job["status"] == "SUCCEEDED", job["error_data"]
        variant = next(
            item for item in client.get(f"/shots/{shot_id}/variants").json()
            if item["provider"] == "producer_upload"
        )
        imported_ids.append(variant["id"])
        assert client.post(f"/shots/{shot_id}/variants/{variant['id']}/select").status_code == 200

    timeline = client.get(f"/projects/{project_id}/tour-timeline").json()
    assert timeline["ready"] is True
    assert timeline["total_duration"] == 40
    render = client.post(
        f"/projects/{project_id}/tour-renders",
        json={"opening_title": "Beograd iz prvog lica", "closing_title": "Kraj ture"},
    )
    assert render.status_code == 202
    render_id = render.json()["id"]
    spec = client.get(f"/renders/{render_id}").json()["render_spec"]
    assert [item["variant_id"] for item in spec["shots"]] == imported_ids
    assert all(item["variant_origin"] == "producer_upload" for item in spec["shots"])
    assert all(item["original_asset_id"] and item["original_checksum"] for item in spec["shots"])
    completed = wait_for(
        lambda: client.get(f"/renders/{render_id}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=60,
    )
    assert completed["status"] == "SUCCEEDED", completed["error_data"]
    output = root / completed["output_storage_key"]
    probe = subprocess.run(
        [settings.ffprobe_executable, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(output)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    metadata = json.loads(probe.stdout)
    streams = {stream["codec_type"]: stream for stream in metadata["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == (720, 1280)
    assert streams["audio"]["codec_name"] == "aac"
    assert float(metadata["format"]["duration"]) == pytest.approx(40, abs=0.15)
    assert "mp4" in metadata["format"]["format_name"]
    expected_dominant = ({0}, {1}, {2}, {0, 1}, {0, 2})
    for index, dominant in enumerate(expected_dominant):
        frame = subprocess.run(
            [
                settings.ffmpeg_executable,
                "-v",
                "error",
                "-ss",
                str(index * 8 + 4),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        mean = ImageStat.Stat(Image.open(BytesIO(frame))).mean[:3]
        assert min(mean[channel] for channel in dominant) > max(
            mean[channel] for channel in set(range(3)) - dominant
        ) + 20
        assert estimate_frequency(output, index * 8 + 0.08) == pytest.approx(scenes[index][1], abs=100)
