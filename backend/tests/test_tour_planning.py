from __future__ import annotations

import io
import wave
from copy import deepcopy

import pytest

from backend.app.config import settings
from backend.app.domain.planning import PlanningRequest, validate_plan
from backend.app.providers.mock_planning import MockPlanningProvider
from backend.tests.test_api_integration import submit_generation


def tour_request() -> PlanningRequest:
    return PlanningRequest(
        project_id="tour-1",
        project_type="TOUR_GUIDE",
        project_title="Beograd POV",
        creative_brief=(
            "Beograd, vertikalni 9:16, 30–45 sekundi, POV, srpski jezik, "
            "kratka naracija po sceni, uvodni naslov i završni kadar."
        ),
        target_duration_seconds=40,
        aspect_ratio="9:16",
        visual_tone="authentic urban",
        narrative_approach="first-person route",
        performance_presence="POV host",
        pacing="brisk",
        constraints="Serbian narration",
        maximum_shot_count=5,
        correlation_id="tour-correlation",
        idempotency_key="tour-idempotency",
    )


def voiceover_wav(duration: float) -> bytes:
    output = io.BytesIO()
    sample_rate = 8_000
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\0\0" * int(sample_rate * duration))
    return output.getvalue()


def test_mock_tour_plan_links_scenes_narration_and_source_state():
    provider = MockPlanningProvider(delay_seconds=0)
    task_id = provider.submit_plan(tour_request())
    result = provider.get_result(task_id)

    assert result.schema_version == "tour-guide-plan-v1"
    assert len(result.shots) == 5
    assert result.shots[0].location_or_motif == "Uvodni naslov"
    assert result.shots[-1].location_or_motif == "Završni kadar"
    assert all(scene.pov_description and scene.narration for scene in result.shots)
    assert sum(scene.duration_seconds for scene in result.shots) == pytest.approx(40)
    with pytest.raises(ValueError, match="without sources"):
        validate_plan(result, target_duration=40, maximum_shots=5, allowed_asset_ids=set())
    validate_plan(
        result,
        target_duration=40,
        maximum_shots=5,
        allowed_asset_ids=set(),
        require_verified_sources=False,
    )


@pytest.mark.integration
def test_tour_plan_version_approval_and_music_video_isolation(
    client, db, worker_process, wait_for
):
    invalid_brief = client.post(
        "/projects",
        json={
            "project_type": "TOUR_GUIDE",
            "title": "Too short",
            "creative_brief": "Belgrade",
            "aspect_ratio": "9:16",
        },
    )
    assert invalid_brief.status_code == 422
    wrong_format = client.post(
        "/projects",
        json={
            "project_type": "TOUR_GUIDE",
            "title": "Wrong format",
            "creative_brief": "A sufficiently detailed vertical travel brief",
            "aspect_ratio": "16:9",
        },
    )
    assert wrong_format.status_code == 422

    project = client.post(
        "/projects",
        json={
            "project_type": "TOUR_GUIDE",
            "title": "Beograd POV",
            "creative_brief": tour_request().creative_brief,
            "aspect_ratio": "9:16",
        },
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    invalid_duration = client.post(
        f"/projects/{project_id}/plans/generate",
        json={
            "target_duration_seconds": 20,
            "visual_tone": "urban",
            "narrative_approach": "POV",
            "performance_presence": "host",
            "pacing": "brisk",
            "constraints": "Serbian narration",
            "maximum_shot_count": 5,
            "idempotency_key": "tour-invalid-duration",
        },
    )
    assert invalid_duration.status_code == 422

    submitted = client.post(
        f"/projects/{project_id}/plans/generate",
        json={
            "target_duration_seconds": 40,
            "visual_tone": "urban",
            "narrative_approach": "POV",
            "performance_presence": "host",
            "pacing": "brisk",
            "constraints": "Serbian narration",
            "maximum_shot_count": 5,
            "idempotency_key": "tour-valid-plan",
        },
    )
    assert submitted.status_code == 202
    worker_process(planning_delay=0)
    job_id = submitted.json()["job_id"]
    wait_for(
        lambda: client.get(f"/jobs/{job_id}").json(),
        lambda value: value["status"] == "SUCCEEDED",
        timeout=10,
    )
    summary = client.get(f"/projects/{project_id}/plans").json()[0]
    original = client.get(f"/projects/{project_id}/plans/{summary['id']}").json()
    assert original["version"] == 1
    assert original["model"] == "mock-tour-guide-planner-v1"
    assert client.post(f"/projects/{project_id}/plans/{original['id']}/approve").status_code == 422

    revised_body = {
        key: deepcopy(original[key])
        for key in (
            "schema_version",
            "concept_title",
            "logline",
            "treatment",
            "creative_direction",
            "shots",
        )
    }
    missing_claim = next(
        claim
        for scene in revised_body["shots"]
        for claim in scene["factual_claims"]
        if not claim["sources"]
    )
    missing_claim["sources"] = ["https://example.test/editor-verified-source"]
    revised = client.post(
        f"/projects/{project_id}/plans/{original['id']}/versions", json=revised_body
    )
    assert revised.status_code == 201
    assert revised.json()["version"] == 2
    assert revised.json()["parent_plan_id"] == original["id"]
    unapproved_handoff = client.post(
        f"/projects/{project_id}/plans/{revised.json()['id']}/handoff"
    )
    assert unapproved_handoff.status_code == 409

    with db() as conn:
        conn.execute("UPDATE project_plans SET status='APPROVED' WHERE id=%s", (original["id"],))
    blocked_claims = client.post(f"/projects/{project_id}/plans/{original['id']}/handoff")
    assert blocked_claims.status_code == 422
    assert "without sources" in blocked_claims.json()["detail"]
    with db() as conn:
        conn.execute("UPDATE project_plans SET status='DRAFT' WHERE id=%s", (original["id"],))

    approved = client.post(f"/projects/{project_id}/plans/{revised.json()['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["created_shot_ids"] == []
    assert approved.json()["plan_id"] == revised.json()["id"]
    assert client.post(f"/projects/{project_id}/plans/{original['id']}/approve").status_code == 422
    unchanged = client.get(f"/projects/{project_id}/plans/{original['id']}").json()
    assert unchanged["shots"][1]["factual_claims"][0]["sources"] == []

    with db() as conn:
        assert conn.execute("SELECT count(*) FROM shots WHERE project_id=%s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM generation_attempts WHERE project_id=%s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT status FROM project_plans WHERE id=%s", (revised.json()["id"],)).fetchone()[0] == "APPROVED"
        events = conn.execute(
            "SELECT event_type,payload FROM events WHERE project_id=%s ORDER BY created_at",
            (project_id,),
        ).fetchall()
    assert any(event == "PLAN_APPROVED" and payload["version"] == 2 for event, payload in events)
    assert all(event != "PLAN_SHOTS_MATERIALIZED" for event, _payload in events)

    with db() as conn:
        conn.execute(
            "CREATE FUNCTION reject_third_tour_shot() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.source_plan_item_key='scene-003' THEN RAISE EXCEPTION 'controlled'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_third_tour_shot BEFORE INSERT ON shots "
            "FOR EACH ROW EXECUTE FUNCTION reject_third_tour_shot()"
        )
    failed_handoff = client.post(
        f"/projects/{project_id}/plans/{revised.json()['id']}/handoff"
    )
    assert failed_handoff.status_code == 500
    assert failed_handoff.json()["detail"] == (
        "tour plan handoff failed; no production shots were created"
    )
    with db() as conn:
        assert conn.execute(
            "SELECT count(*) FROM shots WHERE source_plan_id=%s", (revised.json()["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT materialized_at FROM project_plans WHERE id=%s", (revised.json()["id"],)
        ).fetchone()[0] is None
        conn.execute("DROP TRIGGER reject_third_tour_shot ON shots")
        conn.execute("DROP FUNCTION reject_third_tour_shot()")

    handoff = client.post(f"/projects/{project_id}/plans/{revised.json()['id']}/handoff")
    assert handoff.status_code == 200
    assert handoff.json()["idempotent"] is False
    shots = client.get(f"/projects/{project_id}/shots").json()
    assert [shot["ordinal"] for shot in shots] == list(range(1, len(shots) + 1))
    assert [shot["source_plan_item_key"] for shot in shots] == [
        scene["item_key"] for scene in revised.json()["shots"]
    ]
    for shot, scene in zip(shots, revised.json()["shots"], strict=True):
        assert shot["source_plan_id"] == revised.json()["id"]
        assert shot["source_plan_version"] == 2
        assert shot["source_scene_duration"] == scene["duration_seconds"]
        assert shot["source_location_or_motif"] == scene["location_or_motif"]
        assert shot["source_pov_description"] == scene["pov_description"]
        assert shot["source_narration"] == scene["narration"]
        assert shot["source_factual_claims"] == scene["factual_claims"]
    repeated = client.post(f"/projects/{project_id}/plans/{revised.json()['id']}/handoff")
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["created_shot_ids"] == handoff.json()["created_shot_ids"]
    with db() as conn:
        conn.execute(
            "UPDATE shots SET source_narration='tampered provenance' WHERE id=%s",
            (shots[0]["id"],),
        )
    inconsistent = client.post(f"/projects/{project_id}/plans/{revised.json()['id']}/handoff")
    assert inconsistent.status_code == 409
    assert "incomplete or inconsistent" in inconsistent.json()["detail"]
    with db() as conn:
        conn.execute(
            "UPDATE shots SET source_narration=%s WHERE id=%s",
            (revised.json()["shots"][0]["narration"], shots[0]["id"]),
        )
    settings.video_provider = "runway"
    mock_only = submit_generation(client, shots[0]["id"], "tour-runway-blocked")
    assert mock_only.status_code == 422
    assert "mock generation only" in mock_only.json()["detail"]
    with db() as conn:
        assert conn.execute(
            "SELECT count(*) FROM jobs WHERE idempotency_key='tour-runway-blocked'"
        ).fetchone()[0] == 0
    settings.video_provider = "mock"
    first_generation = submit_generation(client, shots[0]["id"], "tour-mock-first")
    assert first_generation.status_code == 202
    duplicate = submit_generation(client, shots[0]["id"], "tour-mock-first")
    assert duplicate.status_code == 202
    assert duplicate.json()["deduplicated"] is True
    first_job_id = first_generation.json()["job_id"]
    wait_for(
        lambda: client.get(f"/jobs/{first_job_id}").json(),
        lambda value: value["status"] == "SUCCEEDED",
        timeout=10,
    )
    first_variant = client.get(f"/shots/{shots[0]['id']}/variants").json()[0]
    assert first_variant["provider"] == "mock"
    assert first_variant["model"] == "mock-video-v1"
    assert first_variant["provenance"]["aspect_ratio"] == "9:16"
    assert first_variant["plan_provenance"] == {
        "source_plan_id": revised.json()["id"],
        "source_plan_version": 2,
        "source_plan_item_key": revised.json()["shots"][0]["item_key"],
    }
    assert client.post(
        f"/shots/{shots[0]['id']}/variants/{first_variant['id']}/select"
    ).status_code == 200
    assert client.get(f"/projects/{project_id}/shots").json()[0][
        "selected_variant_id"
    ] == first_variant["id"]
    assert client.post(
        f"/shots/{shots[0]['id']}/variants/{first_variant['id']}/reject"
    ).status_code == 200
    second_generation = submit_generation(client, shots[0]["id"], "tour-mock-second")
    assert second_generation.status_code == 202
    second_job_id = second_generation.json()["job_id"]
    wait_for(
        lambda: client.get(f"/jobs/{second_job_id}").json(),
        lambda value: value["status"] == "SUCCEEDED",
        timeout=10,
    )
    variants = client.get(f"/shots/{shots[0]['id']}/variants").json()
    assert len(variants) == 2
    assert variants[0]["review_status"] == "REJECTED"
    assert client.post(
        f"/shots/{shots[0]['id']}/variants/{variants[1]['id']}/select"
    ).status_code == 200
    reloaded_shot = client.get(f"/projects/{project_id}/shots").json()[0]
    assert reloaded_shot["selected_variant_id"] == variants[1]["id"]
    invalid_voiceover = client.post(
        f"/shots/{shots[0]['id']}/voiceover",
        files={"file": ("not-audio.wav", b"not audio", "audio/wav")},
    )
    assert invalid_voiceover.status_code == 422
    bypass = client.post(
        f"/projects/{project_id}/assets",
        files={"file": ("bypass.wav", voiceover_wav(0.25), "audio/wav")},
        data={"asset_type": "VOICEOVER"},
    )
    assert bypass.status_code == 422
    long_voiceover = client.post(
        f"/shots/{shots[0]['id']}/voiceover",
        files={
            "file": (
                "long.wav",
                voiceover_wav(float(shots[0]["source_scene_duration"]) + 0.5),
                "audio/wav",
            )
        },
    )
    assert long_voiceover.status_code == 201
    assert long_voiceover.json()["duration"] > shots[0]["source_scene_duration"]
    too_long_timeline = client.get(f"/projects/{project_id}/tour-timeline")
    assert too_long_timeline.status_code == 200
    assert too_long_timeline.json()["items"][0]["voiceover_status"] == "TOO_LONG"

    for index, shot in enumerate(shots):
        if index:
            generation = submit_generation(client, shot["id"], f"tour-voice-{index}")
            assert generation.status_code == 202
            job_id = generation.json()["job_id"]
            wait_for(
                lambda job_id=job_id: client.get(f"/jobs/{job_id}").json(),
                lambda value: value["status"] == "SUCCEEDED",
                timeout=10,
            )
            variant = client.get(f"/shots/{shot['id']}/variants").json()[0]
            assert client.post(
                f"/shots/{shot['id']}/variants/{variant['id']}/select"
            ).status_code == 200
        voiceover = client.post(
            f"/shots/{shot['id']}/voiceover",
            files={"file": (f"scene-{index + 1}.wav", voiceover_wav(0.25), "audio/wav")},
        )
        assert voiceover.status_code == 201
        assert voiceover.json()["duration"] == pytest.approx(0.25, abs=0.02)
    timeline = client.get(f"/projects/{project_id}/tour-timeline").json()
    assert timeline["ready"] is True
    assert timeline["total_duration"] == pytest.approx(40)
    assert timeline["items"][0]["start_seconds"] == 0
    assert all(
        current["start_seconds"] == pytest.approx(previous["end_seconds"])
        for previous, current in zip(
            timeline["items"][:-1], timeline["items"][1:], strict=True
        )
    )
    assert timeline["items"][-1]["end_seconds"] == pytest.approx(40)
    assert all(item["voiceover_status"] == "READY" for item in timeline["items"])
    assert timeline["next_render_requirements"] == [
        "opening title",
        "closing title",
        "music bed",
    ]
    assets = client.get(f"/projects/{project_id}/assets").json()
    voiceovers = [asset for asset in assets if asset["asset_type"] == "VOICEOVER"]
    assert len(voiceovers) == len(shots)
    assert all(asset["provenance_current"] is True for asset in voiceovers)
    assert client.get(f"/assets/{voiceovers[0]['id']}/media").status_code == 200
    with db() as conn:
        materialized = conn.execute(
            "SELECT materialized_at FROM project_plans WHERE id=%s", (revised.json()["id"],)
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT payload FROM events WHERE event_type='TOUR_PLAN_SHOTS_MATERIALIZED' "
            "AND entity_id=%s",
            (revised.json()["id"],),
        ).fetchone()[0]
    assert materialized is not None
    assert audit["version"] == 2
    assert audit["shot_ids"] == handoff.json()["created_shot_ids"]

    replacement_body = deepcopy(revised_body)
    replacement_body["concept_title"] = "Producer replacement"
    replacement = client.post(
        f"/projects/{project_id}/plans/{revised.json()['id']}/versions", json=replacement_body
    )
    assert replacement.status_code == 201
    assert client.post(
        f"/projects/{project_id}/plans/{replacement.json()['id']}/approve"
    ).status_code == 200
    blocked_replacement = client.post(
        f"/projects/{project_id}/plans/{replacement.json()['id']}/handoff"
    )
    assert blocked_replacement.status_code == 409
    assert "generation or producer review history" in blocked_replacement.json()["detail"]
    assert [shot["id"] for shot in client.get(f"/projects/{project_id}/shots").json()] == [
        shot["id"] for shot in shots
    ]
    invalid_generation = submit_generation(client, shots[0]["id"], "superseded-tour-shot")
    assert invalid_generation.status_code == 409
    assert "current approved plan" in invalid_generation.json()["detail"]
    stale_timeline = client.get(f"/projects/{project_id}/tour-timeline").json()
    assert stale_timeline["ready"] is False
    assert all(item["voiceover_status"] == "STALE" for item in stale_timeline["items"])
    stale_assets = client.get(f"/projects/{project_id}/assets").json()
    assert all(
        asset["provenance_current"] is False
        for asset in stale_assets
        if asset["asset_type"] == "VOICEOVER"
    )
    stale_upload = client.post(
        f"/shots/{shots[0]['id']}/voiceover",
        files={"file": ("stale.wav", voiceover_wav(0.25), "audio/wav")},
    )
    assert stale_upload.status_code == 409

    music = client.post(
        "/projects",
        json={
            "project_type": "MUSIC_VIDEO",
            "title": "Music isolation",
            "creative_brief": "A complete music video creative brief",
            "aspect_ratio": "16:9",
        },
    )
    assert music.status_code == 201
    assert client.get(f"/projects/{music.json()['id']}/plans").status_code == 200
    legacy_music = client.post(
        "/projects",
        json={"project_type": "MUSIC_VIDEO", "title": "Legacy client", "aspect_ratio": "16:9"},
    )
    assert legacy_music.status_code == 201
    manual_music_shot = client.post(
        f"/projects/{legacy_music.json()['id']}/shots",
        json={"ordinal": 1, "title": "Existing shot", "prompt": "Keep", "intended_duration": 2},
    )
    assert manual_music_shot.status_code == 201
    persisted_music_shot = client.get(
        f"/projects/{legacy_music.json()['id']}/shots"
    ).json()[0]
    assert persisted_music_shot["source_plan_version"] is None
    assert persisted_music_shot["source_scene_duration"] is None
    assert persisted_music_shot["source_factual_claims"] == []
