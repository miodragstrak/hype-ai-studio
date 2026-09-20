from __future__ import annotations

import time
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration


def create_project(client, title: str = "Planning test") -> str:
    response = client.post(
        "/projects",
        json={
            "project_type": "MUSIC_VIDEO",
            "title": title,
            "creative_brief": "A precise nocturnal performance with practical light.",
            "aspect_ratio": "16:9",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def generation_body(key: str) -> dict:
    return {
        "target_duration_seconds": 12,
        "visual_tone": "luminous monochrome",
        "narrative_approach": "rising abstraction",
        "performance_presence": "silhouetted performer",
        "pacing": "measured",
        "constraints": "No text or logos.",
        "use_reference_assets": False,
        "maximum_shot_count": 4,
        "idempotency_key": key,
    }


def wait_job(client, job_id: str, terminal: str = "SUCCEEDED", timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            assert job["status"] == terminal, job
            return job
        time.sleep(0.05)
    pytest.fail("planning job did not finish within its bounded timeout")


def generated_plan(client, worker_process, project_id: str, **worker_options) -> dict:
    worker_process(**worker_options)
    response = client.post(
        f"/projects/{project_id}/plans/generate",
        json=generation_body(f"plan-{uuid4()}"),
    )
    assert response.status_code == 202
    wait_job(client, response.json()["job_id"])
    plans = client.get(f"/projects/{project_id}/plans").json()
    assert len(plans) == 1
    return client.get(f"/projects/{project_id}/plans/{plans[0]['id']}").json()


def test_async_submission_crosses_redis_and_separate_worker(client, worker_process, db):
    project_id = create_project(client)
    worker_process(planning_delay=0.45)
    started = time.monotonic()
    response = client.post(
        f"/projects/{project_id}/plans/generate",
        json=generation_body("async-plan"),
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 202
    assert elapsed < 0.3
    assert response.json() == {
        "job_id": response.json()["job_id"],
        "status": "QUEUED",
        "deduplicated": False,
    }
    job = wait_job(client, response.json()["job_id"])
    assert job["started_at"] and job["completed_at"]
    plans = client.get(f"/projects/{project_id}/plans").json()
    assert [(plan["version"], plan["provider"], plan["status"]) for plan in plans] == [
        (1, "mock", "DRAFT")
    ]
    detail = client.get(f"/projects/{project_id}/plans/{plans[0]['id']}").json()
    assert sum(shot["duration_seconds"] for shot in detail["shots"]) == pytest.approx(12)
    assert detail["source_job_id"] == response.json()["job_id"]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM generation_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM shot_variants").fetchone()[0] == 0


def test_generation_idempotency_and_project_scope(client, worker_process):
    first = create_project(client, "First")
    second = create_project(client, "Second")
    worker_process()
    body = generation_body("same-planning-request")
    original = client.post(f"/projects/{first}/plans/generate", json=body)
    duplicate = client.post(f"/projects/{first}/plans/generate", json=body)
    conflict = client.post(f"/projects/{second}/plans/generate", json=body)
    assert original.status_code == duplicate.status_code == 202
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["job_id"] == original.json()["job_id"]
    assert conflict.status_code == 409
    wait_job(client, original.json()["job_id"])
    plan_id = client.get(f"/projects/{first}/plans").json()[0]["id"]
    assert client.get(f"/projects/{second}/plans/{plan_id}").status_code == 404


def test_transient_retry_and_malformed_provider_result(client, worker_process):
    project_id = create_project(client)
    retry_worker = worker_process(planning_failure_mode="transient:1")
    response = client.post(
        f"/projects/{project_id}/plans/generate",
        json=generation_body("retry-plan"),
    )
    job = wait_job(client, response.json()["job_id"])
    assert job["retry_count"] == 1
    retry_worker.terminate()
    retry_worker.wait(timeout=3)

    other = create_project(client, "Malformed")
    worker_process(planning_failure_mode="malformed")
    response = client.post(
        f"/projects/{other}/plans/generate",
        json=generation_body("malformed-plan"),
    )
    failed = wait_job(client, response.json()["job_id"], "FAILED")
    assert failed["retry_count"] == 0
    assert client.get(f"/projects/{other}/plans").json() == []


def test_permanent_provider_failure_is_not_retried(client, worker_process):
    project_id = create_project(client)
    worker_process(planning_failure_mode="permanent")
    response = client.post(
        f"/projects/{project_id}/plans/generate",
        json=generation_body("permanent-failure"),
    )
    failed = wait_job(client, response.json()["job_id"], "FAILED")
    assert failed["retry_count"] == 0
    assert failed["error_data"]["retryable"] is False
    assert client.get(f"/projects/{project_id}/plans").json() == []


def test_versioning_approval_and_materialization_are_atomic_and_idempotent(
    client, worker_process, db
):
    project_id = create_project(client)
    plan = generated_plan(client, worker_process, project_id)
    edited = {
        key: plan[key]
        for key in (
            "schema_version",
            "concept_title",
            "logline",
            "treatment",
            "creative_direction",
            "shots",
        )
    }
    edited["concept_title"] = "Producer revision"
    version = client.post(
        f"/projects/{project_id}/plans/{plan['id']}/versions", json=edited
    )
    assert version.status_code == 201
    revised = version.json()
    assert revised["version"] == 2
    assert revised["parent_plan_id"] == plan["id"]
    assert revised["provider"] == "producer"

    approval = client.post(f"/projects/{project_id}/plans/{revised['id']}/approve")
    assert approval.status_code == 200
    assert approval.json()["idempotent"] is False
    shot_ids = approval.json()["created_shot_ids"]
    repeated = client.post(f"/projects/{project_id}/plans/{revised['id']}/approve")
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["created_shot_ids"] == shot_ids
    shots = client.get(f"/projects/{project_id}/shots").json()
    assert [shot["source_plan_item_key"] for shot in shots] == [
        shot["item_key"] for shot in revised["shots"]
    ]
    assert all(shot["source_plan_id"] == revised["id"] for shot in shots)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM generation_attempts").fetchone()[0] == 0
        events = {
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM events WHERE project_id=%s", (project_id,)
            )
        }
    assert {"PLAN_GENERATED", "PLAN_VERSION_CREATED", "PLAN_APPROVED", "PLAN_SHOTS_MATERIALIZED"} <= events


def test_approval_preserves_manual_shots_and_controls_replacement(client, worker_process, db):
    project_id = create_project(client)
    manual = client.post(
        f"/projects/{project_id}/shots",
        json={"ordinal": 1, "title": "Manual", "prompt": "Keep me", "intended_duration": 1},
    )
    assert manual.status_code == 201
    first = generated_plan(client, worker_process, project_id)
    assert client.post(f"/projects/{project_id}/plans/{first['id']}/approve").status_code == 200
    second_body = {
        key: first[key]
        for key in (
            "schema_version",
            "concept_title",
            "logline",
            "treatment",
            "creative_direction",
            "shots",
        )
    }
    second_body["concept_title"] = "Replacement"
    second = client.post(
        f"/projects/{project_id}/plans/{first['id']}/versions", json=second_body
    ).json()
    replacement = client.post(f"/projects/{project_id}/plans/{second['id']}/approve")
    assert replacement.status_code == 200
    shots = client.get(f"/projects/{project_id}/shots").json()
    assert shots[0]["id"] == manual.json()["id"]
    assert shots[0]["source_plan_id"] is None
    assert all(shot["source_plan_id"] == second["id"] for shot in shots[1:])
    assert client.get(f"/projects/{project_id}/plans/{first['id']}").json()["status"] == "SUPERSEDED"

    with db() as conn:
        conn.execute("UPDATE shots SET title='Producer changed this' WHERE source_plan_id=%s", (second["id"],))
    third = client.post(
        f"/projects/{project_id}/plans/{second['id']}/versions", json=second_body
    ).json()
    blocked = client.post(f"/projects/{project_id}/plans/{third['id']}/approve")
    assert blocked.status_code == 409
    assert client.get(f"/projects/{project_id}/plans/{second['id']}").json()["status"] == "APPROVED"


def test_validation_and_not_found_responses(client):
    missing = uuid4()
    assert client.get(f"/projects/{missing}/plans").status_code == 404
    project_id = create_project(client)
    invalid = generation_body("invalid")
    invalid["target_duration_seconds"] = 0
    assert client.post(f"/projects/{project_id}/plans/generate", json=invalid).status_code == 422
    assert client.get(f"/projects/{project_id}/plans/{uuid4()}").status_code == 404


def test_database_constraints_and_snapshot_immutability(client, worker_process, db):
    project_id = create_project(client)
    plan = generated_plan(client, worker_process, project_id)
    with db() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE project_plans SET treatment='mutated' WHERE id=%s", (plan["id"],))
        conn.rollback()
        assert conn.execute(
            "SELECT treatment FROM project_plans WHERE id=%s", (plan["id"],)
        ).fetchone()[0] == plan["treatment"]


def test_materialization_rolls_back_completely_on_insert_failure(client, worker_process, db):
    project_id = create_project(client)
    plan = generated_plan(client, worker_process, project_id)
    with db() as conn:
        conn.execute(
            "CREATE FUNCTION reject_second_plan_shot() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.source_plan_item_key='shot-002' THEN RAISE EXCEPTION 'controlled'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_second_plan_shot BEFORE INSERT ON shots "
            "FOR EACH ROW EXECUTE FUNCTION reject_second_plan_shot()"
        )
    with pytest.raises(Exception, match="controlled"):
        client.post(f"/projects/{project_id}/plans/{plan['id']}/approve")
    with db() as conn:
        assert conn.execute(
            "SELECT count(*) FROM shots WHERE source_plan_id=%s", (plan["id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM project_plans WHERE id=%s", (plan["id"],)
        ).fetchone()[0] == "DRAFT"
        conn.execute("DROP TRIGGER reject_second_plan_shot ON shots")
        conn.execute("DROP FUNCTION reject_second_plan_shot()")


def test_replacement_is_blocked_after_generation_history(client, worker_process, db):
    project_id = create_project(client)
    plan = generated_plan(client, worker_process, project_id)
    client.post(f"/projects/{project_id}/plans/{plan['id']}/approve").raise_for_status()
    shot_id = client.get(f"/projects/{project_id}/shots").json()[0]["id"]
    with db() as conn:
        job_id = conn.execute(
            "INSERT INTO jobs(project_id,shot_id,job_type,status,idempotency_key) "
            "VALUES (%s,%s,'SHOT_GENERATION','SUCCEEDED',%s) RETURNING id",
            (project_id, shot_id, f"history-{uuid4()}"),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO generation_attempts(project_id,shot_id,job_id,provider,model,"
            "submitted_prompt,attempt_number,status) VALUES (%s,%s,%s,'mock','mock','x',1,'SUCCEEDED')",
            (project_id, shot_id, job_id),
        )
    body = {
        key: plan[key]
        for key in (
            "schema_version",
            "concept_title",
            "logline",
            "treatment",
            "creative_direction",
            "shots",
        )
    }
    body["concept_title"] = "Blocked replacement"
    replacement = client.post(
        f"/projects/{project_id}/plans/{plan['id']}/versions", json=body
    ).json()
    response = client.post(f"/projects/{project_id}/plans/{replacement['id']}/approve")
    assert response.status_code == 409
    assert "generation has started" in response.json()["detail"]
