import pytest
from pydantic import ValidationError

from backend.app.domain.planning import PlanningRequest, validate_plan
from backend.app.providers.mock_planning import MockPlanningProvider


def request() -> PlanningRequest:
    return PlanningRequest(
        project_id="project",
        creative_brief="A practical-light performance builds into abstraction.",
        target_duration_seconds=17,
        aspect_ratio="16:9",
        visual_tone="silver nocturne",
        narrative_approach="escalating visual motif",
        performance_presence="one imaginary performer",
        pacing="measured",
        constraints="No logos.",
        reference_assets=[{"id": "asset-1", "mime_type": "image/png"}],
        maximum_shot_count=4,
        correlation_id="correlation",
        idempotency_key="idempotency",
    )


def test_request_validation_and_deterministic_mock_result():
    first_provider = MockPlanningProvider(delay_seconds=0)
    first = first_provider.submit_plan(request())
    second_provider = MockPlanningProvider(delay_seconds=0)
    second = second_provider.submit_plan(request())
    result = first_provider.get_result(first)
    repeated = second_provider.get_result(second)
    assert result == repeated
    assert len(result.shots) == 4
    assert sum(shot.duration_seconds for shot in result.shots) == pytest.approx(17)
    assert "No logos" in result.treatment
    assert all(shot.reference_asset_ids == ["asset-1"] for shot in result.shots)
    assert validate_plan(
        result, target_duration=17, maximum_shots=4, allowed_asset_ids={"asset-1"}
    ) == result


def test_request_and_result_validation_reject_bad_data():
    with pytest.raises(ValidationError):
        PlanningRequest.model_validate({})
    provider = MockPlanningProvider(delay_seconds=0)
    task = provider.submit_plan(request())
    result = provider.get_result(task)
    result.shots[0].reference_asset_ids = ["another-project"]
    with pytest.raises(ValueError, match="outside this project"):
        validate_plan(result, target_duration=17, maximum_shots=4, allowed_asset_ids=set())
