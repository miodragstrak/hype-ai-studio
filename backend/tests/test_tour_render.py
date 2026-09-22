from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import struct
import subprocess
import time
import wave
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image, ImageStat
from psycopg.types.json import Jsonb

from backend.app.config import settings
from backend.app.queue import enqueue


def tone_wav(duration: float, frequency: float) -> bytes:
    output = io.BytesIO()
    rate = 8_000
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        frames = [
            int(12_000 * math.sin(2 * math.pi * frequency * index / rate))
            for index in range(int(rate * duration))
        ]
        audio.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return output.getvalue()


def make_video(path: Path, color: str, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            settings.ffmpeg_executable,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=180x320:r=25:d={duration}",
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


def seed_ready_tour(
    db,
    storage_root: Path,
    scenes: tuple[tuple[str, float], ...] = (("red", 440.0), ("blue", 660.0)),
    scene_duration: float = 1,
    voice_duration: float = 0.55,
) -> tuple[str, list[str]]:
    project_id, plan_id = uuid4(), uuid4()
    shot_ids: list[str] = []
    with db() as conn:
        conn.execute(
            "INSERT INTO projects(id,project_type,title,creative_brief,aspect_ratio) VALUES (%s,'TOUR_GUIDE','Render tour','A complete vertical tour guide production brief','9:16')",
            (project_id,),
        )
        conn.execute(
            "INSERT INTO project_plans(id,project_id,version,status,planning_inputs,concept_title,treatment,creative_direction,shot_plan,provider,model,prompt_schema_version,approved_at,materialized_at) VALUES (%s,%s,1,'APPROVED','{}','Tour','Treatment','{}','[]','mock','mock-tour-guide-planner-v1','tour-guide-plan-v1',now(),now())",
            (plan_id, project_id),
        )
        for ordinal, (color, frequency) in enumerate(scenes, 1):
            shot_id, job_id, attempt_id, variant_id, voice_id = (uuid4() for _ in range(5))
            shot_ids.append(str(shot_id))
            item_key = f"scene-{ordinal:03d}"
            narration = f"Narration {ordinal}"
            variant_key = f"projects/{project_id}/variants/{variant_id}.mp4"
            voice_key = f"projects/{project_id}/voiceovers/{shot_id}.wav"
            make_video(storage_root / variant_key, color, scene_duration + 0.2)
            voice = tone_wav(voice_duration, frequency)
            voice_path = storage_root / voice_key
            voice_path.parent.mkdir(parents=True, exist_ok=True)
            voice_path.write_bytes(voice)
            conn.execute(
                "INSERT INTO shots(id,project_id,ordinal,title,prompt,intended_duration,source_plan_id,source_plan_item_key,source_plan_version,source_scene_duration,source_location_or_motif,source_pov_description,source_narration,source_factual_claims) VALUES (%s,%s,%s,%s,'POV',%s,%s,%s,1,%s,'Belgrade','POV walk',%s,'[]')",
                (
                    shot_id,
                    project_id,
                    ordinal,
                    f"Scene {ordinal}",
                    scene_duration,
                    plan_id,
                    item_key,
                    scene_duration,
                    narration,
                ),
            )
            conn.execute(
                "INSERT INTO jobs(id,project_id,shot_id,job_type,status,idempotency_key) VALUES (%s,%s,%s,'GENERATION','SUCCEEDED',%s)",
                (job_id, project_id, shot_id, f"tour-render-{ordinal}-{project_id}"),
            )
            conn.execute(
                "INSERT INTO generation_attempts(id,project_id,shot_id,job_id,provider,model,submitted_prompt,parameters,attempt_number,status) VALUES (%s,%s,%s,%s,'mock','mock-video-v1','POV',%s,1,'SUCCEEDED')",
                (
                    attempt_id,
                    project_id,
                    shot_id,
                    job_id,
                    Jsonb(
                        {
                            "tour_provenance": {
                                "source_plan_id": str(plan_id),
                                "source_plan_version": 1,
                                "source_plan_item_key": item_key,
                            }
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO shot_variants(id,shot_id,generation_attempt_id,storage_key,mime_type,duration,review_status) VALUES (%s,%s,%s,%s,'video/mp4',%s,'SELECTED')",
                (variant_id, shot_id, attempt_id, variant_key, scene_duration + 0.2),
            )
            conn.execute(
                "UPDATE shots SET selected_variant_id=%s WHERE id=%s", (variant_id, shot_id)
            )
            checksum = hashlib.sha256(voice).hexdigest()
            conn.execute(
                "INSERT INTO assets(id,project_id,asset_type,storage_key,mime_type,size_bytes,checksum,rights_metadata,shot_id,source_plan_id,source_plan_version,source_plan_item_key,narration_checksum,media_duration) VALUES (%s,%s,'VOICEOVER',%s,'audio/wav',%s,%s,'{}',%s,%s,1,%s,%s,%s)",
                (
                    voice_id,
                    project_id,
                    voice_key,
                    len(voice),
                    checksum,
                    shot_id,
                    plan_id,
                    item_key,
                    hashlib.sha256(narration.encode()).hexdigest(),
                    voice_duration,
                ),
            )
    return str(project_id), shot_ids


def estimate_frequency(path: Path, start: float) -> float:
    decoded = subprocess.run(
        [
            settings.ffmpeg_executable,
            "-v",
            "error",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-t",
            "0.35",
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
    return crossings / (2 * (len(samples) / 8000))


TOUR_TITLES = {"opening_title": "Beograd POV", "closing_title": "Kraj ture"}


@pytest.mark.integration
def test_tour_render_real_ffmpeg_and_provenance(
    client, db, worker_process, wait_for, integration_environment
):
    project_id, shot_ids = seed_ready_tour(db, Path(integration_environment["storage_root"]))
    music = tone_wav(0.8, 110)
    upload = client.post(
        f"/projects/{project_id}/tour-music",
        files={"file": ("licensed.wav", music, "audio/wav")},
        data={
            "rights_metadata": json.dumps({"usage_confirmed": True, "license": "synthetic test"})
        },
    )
    assert upload.status_code == 201
    response = client.post(
        f"/projects/{project_id}/tour-renders",
        json={
            "opening_title": "Beograd iz prvog lica",
            "closing_title": "Kraj ture",
            "music_asset_id": upload.json()["id"],
        },
    )
    assert response.status_code == 202
    render_id = response.json()["id"]
    spec = client.get(f"/renders/{render_id}").json()["render_spec"]
    assert spec["source_plan_version"] == 1
    assert [shot["shot_id"] for shot in spec["shots"]] == shot_ids
    assert all(shot["variant_checksum"] and shot["voiceover_checksum"] for shot in spec["shots"])
    assert spec["incomplete_demo_preview"] is False
    worker_process()
    render = wait_for(
        lambda: client.get(f"/renders/{render_id}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=20,
    )
    assert render["status"] == "SUCCEEDED", render["error_data"]
    output = Path(integration_environment["storage_root"]) / render["output_storage_key"]
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
    streams = {stream["codec_type"]: stream for stream in metadata["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == (720, 1280)
    assert streams["audio"]["codec_name"] == "aac"
    assert float(metadata["format"]["duration"]) == pytest.approx(2, abs=0.12)
    assert estimate_frequency(output, 0.08) == pytest.approx(440, abs=90)
    assert estimate_frequency(output, 1.08) == pytest.approx(660, abs=100)
    colors = []
    for position in (0.5, 1.5):
        frame = subprocess.run(
            [
                settings.ffmpeg_executable,
                "-v",
                "error",
                "-ss",
                str(position),
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
            timeout=5,
        ).stdout
        with Image.open(io.BytesIO(frame)) as image:
            colors.append(ImageStat.Stat(image.convert("RGB")).mean)
    assert colors[0][0] > colors[0][2]
    assert colors[1][2] > colors[1][0]
    assert client.get(f"/renders/{render_id}/media").status_code == 200
    assert (
        "attachment"
        in client.get(f"/renders/{render_id}/media?download=true").headers["content-disposition"]
    )
    enqueue(render_id, "render")
    time.sleep(0.5)
    duplicate = client.get(f"/renders/{render_id}").json()
    assert duplicate["status"] == "SUCCEEDED"
    assert duplicate["output_storage_key"] == render["output_storage_key"]
    print(
        "tour ffprobe="
        + json.dumps(
            {
                "format_name": metadata["format"]["format_name"],
                "duration": metadata["format"]["duration"],
                "size": metadata["format"]["size"],
                "video": {
                    key: streams["video"][key]
                    for key in ("codec_name", "width", "height", "r_frame_rate")
                },
                "audio": {
                    key: streams["audio"][key] for key in ("codec_name", "sample_rate", "channels")
                },
            },
            sort_keys=True,
        )
    )


@pytest.mark.integration
def test_tour_render_blocks_missing_and_stale_inputs(
    client, db, worker_process, wait_for, integration_environment
):
    project_id, shot_ids = seed_ready_tour(db, Path(integration_environment["storage_root"]))
    assert (
        client.post(
            f"/projects/{project_id}/tour-renders",
            json={"opening_title": " ", "closing_title": "End"},
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/shots/{shot_ids[0]}",
            json={"ordinal": 1, "title": "Changed", "prompt": "Changed", "intended_duration": 2},
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/projects/{project_id}/shots/order", json={"shot_ids": list(reversed(shot_ids))}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/projects/{project_id}/renders",
            json={"variant_ids": [str(uuid4())], "audio_asset_id": str(uuid4())},
        ).status_code
        == 422
    )
    with db() as conn:
        variant_key = conn.execute(
            "SELECT storage_key FROM shot_variants WHERE shot_id=%s", (shot_ids[0],)
        ).fetchone()[0]
    video_upload = client.post(
        f"/shots/{shot_ids[0]}/voiceover",
        files={
            "file": (
                "video.mp4",
                (Path(integration_environment["storage_root"]) / variant_key).read_bytes(),
                "audio/mp4",
            )
        },
    )
    assert video_upload.status_code == 422
    assert "audio stream" in video_upload.json()["detail"]
    with db() as conn:
        conn.execute("UPDATE shots SET selected_variant_id=NULL WHERE id=%s", (shot_ids[0],))
    missing_variant = client.post(f"/projects/{project_id}/tour-renders", json=TOUR_TITLES)
    assert missing_variant.status_code == 409
    assert "no selected variant" in missing_variant.json()["detail"]
    with db() as conn:
        variant_id = conn.execute(
            "SELECT id FROM shot_variants WHERE shot_id=%s", (shot_ids[0],)
        ).fetchone()[0]
        conn.execute(
            "UPDATE shots SET selected_variant_id=%s WHERE id=%s", (variant_id, shot_ids[0])
        )
        conn.execute(
            "DELETE FROM assets WHERE shot_id=%s AND asset_type='VOICEOVER'", (shot_ids[0],)
        )
    missing_voice = client.post(f"/projects/{project_id}/tour-renders", json=TOUR_TITLES)
    assert missing_voice.status_code == 409
    assert "no voiceover" in missing_voice.json()["detail"]

    project_id, shot_ids = seed_ready_tour(db, Path(integration_environment["storage_root"]))
    queued = client.post(f"/projects/{project_id}/tour-renders", json=TOUR_TITLES)
    assert queued.status_code == 202
    with db() as conn:
        conn.execute("UPDATE shots SET selected_variant_id=NULL WHERE id=%s", (shot_ids[0],))
    stale_worker = worker_process()
    failed = wait_for(
        lambda: client.get(f"/renders/{queued.json()['id']}").json(),
        lambda value: value["status"] == "FAILED",
        timeout=10,
    )
    assert "selected variant" in failed["error_data"]["message"]
    assert client.get(f"/renders/{queued.json()['id']}/media").status_code == 409
    stale_worker.terminate()
    stale_worker.wait(timeout=3)

    project_id, _ = seed_ready_tour(db, Path(integration_environment["storage_root"]))
    queued = client.post(f"/projects/{project_id}/tour-renders", json=TOUR_TITLES)
    assert queued.status_code == 202
    worker_process(extra_env={"FFMPEG_EXECUTABLE": "/missing/ffmpeg"})
    failed = wait_for(
        lambda: client.get(f"/renders/{queued.json()['id']}").json(),
        lambda value: value["status"] == "FAILED",
        timeout=10,
    )
    assert failed["output_storage_key"] is None
    assert client.get(f"/renders/{queued.json()['id']}/media").status_code == 409


@pytest.mark.integration
def test_five_scene_tour_demo_output(client, db, worker_process, wait_for, integration_environment):
    scenes = (
        ("red", 440.0),
        ("green", 500.0),
        ("blue", 560.0),
        ("yellow", 620.0),
        ("magenta", 680.0),
    )
    project_id, _ = seed_ready_tour(
        db,
        Path(integration_environment["storage_root"]),
        scenes=scenes,
        scene_duration=8,
        voice_duration=3,
    )
    music = tone_wav(12, 110)
    upload = client.post(
        f"/projects/{project_id}/tour-music",
        files={"file": ("synthetic-music.wav", music, "audio/wav")},
        data={
            "rights_metadata": json.dumps(
                {"usage_confirmed": True, "license": "generated synthetic fixture"}
            )
        },
    )
    assert upload.status_code == 201
    queued = client.post(
        f"/projects/{project_id}/tour-renders",
        json={**TOUR_TITLES, "music_asset_id": upload.json()["id"]},
    )
    assert queued.status_code == 202
    worker_process()
    render = wait_for(
        lambda: client.get(f"/renders/{queued.json()['id']}").json(),
        lambda value: value["status"] in {"SUCCEEDED", "FAILED"},
        timeout=90,
    )
    assert render["status"] == "SUCCEEDED", render["error_data"]
    output = Path(integration_environment["storage_root"]) / render["output_storage_key"]
    assert float(render["duration"]) == pytest.approx(40, abs=0.15)
    for index, (_, frequency) in enumerate(scenes):
        assert estimate_frequency(output, index * 8 + 0.1) == pytest.approx(frequency, abs=110)
    if target := os.environ.get("TOUR_DEMO_OUTPUT"):
        preserved = Path(target)
        preserved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, preserved)
        print(f"five-scene-tour-output={preserved} duration={render['duration']}")
