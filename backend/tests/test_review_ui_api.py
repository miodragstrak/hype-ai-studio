from __future__ import annotations

import json
from uuid import uuid4

import pytest

from backend.tests.test_api_integration import create_project, create_shot, submit_generation

pytestmark = pytest.mark.integration


def test_project_asset_and_shot_management_endpoints(client):
    project_id = create_project(client, "Producer console")
    other_id = create_project(client, "Second project")
    projects = client.get("/projects")
    assert projects.status_code == 200
    assert [item["id"] for item in projects.json()] == [other_id, project_id]

    upload = client.post(
        f"/projects/{project_id}/assets",
        files={"file": ("reference.png", b"image-bytes", "image/png")},
        data={
            "asset_type": "REFERENCE_IMAGE",
            "rights_metadata": json.dumps(
                {"source": "Producer", "usage_confirmed": True, "note": "Internal test"}
            ),
        },
    )
    assert upload.status_code == 201
    asset_id = upload.json()["id"]
    assets = client.get(f"/projects/{project_id}/assets")
    assert assets.status_code == 200
    assert assets.json()[0]["filename"] == "reference.png"
    assert assets.json()[0]["rights_metadata"]["usage_confirmed"] is True
    media = client.get(f"/assets/{asset_id}/media")
    assert media.status_code == 200
    assert media.content == b"image-bytes"
    assert media.headers["content-type"] == "image/png"
    download = client.get(f"/assets/{asset_id}/media", params={"download": "true"})
    assert "attachment" in download.headers["content-disposition"]

    first = create_shot(client, project_id, ordinal=1)
    second = create_shot(client, project_id, ordinal=2)
    updated = client.put(
        f"/shots/{first}",
        json={
            "ordinal": 1,
            "title": "Opening revised",
            "prompt": "Revised prompt",
            "intended_duration": 2.5,
        },
    )
    assert updated.status_code == 200
    reordered = client.put(
        f"/projects/{project_id}/shots/order", json={"shot_ids": [second, first]}
    )
    assert reordered.status_code == 200
    shots = client.get(f"/projects/{project_id}/shots").json()
    assert [shot["id"] for shot in shots] == [second, first]
    assert shots[1]["title"] == "Opening revised"
    assert shots[1]["latest_job_id"] is None
    invalid_order = client.put(f"/projects/{project_id}/shots/order", json={"shot_ids": [first]})
    assert invalid_order.status_code == 422

    project = client.get(f"/projects/{project_id}").json()
    assert project["summary"] == {
        "assets": 1,
        "shots": 2,
        "selected_variants": 0,
        "renders": 0,
    }
    event_types = [
        event["event_type"] for event in client.get(f"/projects/{project_id}/events").json()
    ]
    assert "ASSET_UPLOADED" in event_types
    assert client.get(f"/projects/{uuid4()}/assets").status_code == 404
    assert client.put(f"/shots/{uuid4()}", json=updated.json()).status_code == 404


def test_variant_details_rejection_and_scoped_media(client, worker_process, wait_for):
    project_id = create_project(client)
    shot_id = create_shot(client, project_id, duration=0.25)
    job_id = submit_generation(client, shot_id, "review-ui-variant").json()["job_id"]
    worker_process(delay=0)
    wait_for(
        lambda: client.get(f"/jobs/{job_id}").json(),
        lambda value: value["status"] == "SUCCEEDED",
        timeout=10,
    )
    variants = client.get(f"/shots/{shot_id}/variants").json()
    assert len(variants) == 1
    variant = variants[0]
    assert variant["job_id"] == job_id
    assert variant["generation_attempt_id"]
    assert variant["attempt_number"] == 1
    media = client.get(f"/variants/{variant['id']}/media")
    assert media.status_code == 200
    assert media.headers["content-type"] == "video/mp4"
    assert len(media.content) > 100

    assert client.post(f"/shots/{shot_id}/variants/{variant['id']}/select").status_code == 200
    rejected = client.post(f"/shots/{shot_id}/variants/{variant['id']}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["review_status"] == "REJECTED"
    assert client.get(f"/projects/{project_id}/shots").json()[0]["selected_variant_id"] is None
    assert client.get(f"/shots/{shot_id}/variants").json()[0]["review_status"] == "REJECTED"
    events = client.get(f"/projects/{project_id}/events").json()
    assert any(event["event_type"] == "VARIANT_REJECTED" for event in events)
    assert client.get(f"/variants/{uuid4()}/media").status_code == 404
    assert client.post(f"/shots/{shot_id}/variants/{uuid4()}/reject").status_code == 404


def test_render_listing_and_unavailable_media(client):
    project_id = create_project(client)
    assert client.get(f"/projects/{project_id}/renders").json() == []
    assert client.get(f"/renders/{uuid4()}/media").status_code == 404
