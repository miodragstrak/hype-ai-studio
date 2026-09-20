from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import SecretStr, ValidationError

from backend.app.config import Settings, settings
from backend.app.domain.models import NormalizedStatus
from backend.app.domain.planning import PlanningRequest
from backend.app.providers.openai_planning import (
    OpenAIPlanningError,
    OpenAIPlanningProvider,
    build_planning_prompt,
)
from backend.app.security import REDACTED, sanitize
from backend.app.services.planning_budget import estimate_cost, estimate_tokens


def planning_request() -> PlanningRequest:
    return PlanningRequest(
        project_id="project-1",
        project_title="Silver Signal",
        creative_brief="An imaginary performance builds from darkness into silver light.",
        target_duration_seconds=10,
        aspect_ratio="16:9",
        visual_tone="silver nocturne",
        narrative_approach="escalating abstraction",
        performance_presence="one imaginary performer",
        pacing="measured",
        constraints="No logos or text.",
        reference_assets=[
            {
                "id": "asset-1",
                "asset_type": "REFERENCE_IMAGE",
                "mime_type": "image/png",
                "size_bytes": 42,
                "rights_metadata": {"usage_confirmed": True},
                "binary": "must-not-be-sent",
            }
        ],
        maximum_shot_count=2,
        correlation_id="correlation-1",
        idempotency_key="idempotency-1",
    )


def result_data() -> dict:
    return {
        "schema_version": "music-video-plan-v1",
        "concept_title": "Silver Signal",
        "logline": "A signal becomes a performance.",
        "treatment": "A measured visual progression develops in practical silver light.",
        "creative_direction": {
            "visual_style": "tactile nocturne",
            "color_palette": ["silver", "black"],
            "camera_language": "controlled movement",
            "editing_rhythm": "measured",
            "performance_direction": "restrained",
            "continuity_notes": ["preserve screen direction"],
            "avoid": ["logos"],
        },
        "shots": [
            {
                "item_key": f"shot-{index:03d}",
                "ordinal": index,
                "title": f"Beat {index}",
                "description": "A practical-light visual beat.",
                "prompt": "Cinematic silver performance, no text.",
                "duration_seconds": 5,
                "shot_type": "wide",
                "camera": "slow push",
                "subject": "imaginary performer",
                "environment": "abstract stage",
                "continuity_notes": "Preserve direction.",
                "reference_asset_ids": ["asset-1"],
            }
            for index in (1, 2)
        ],
    }


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_provider(response=None, error=None):
    responses = FakeResponses(response, error)
    client = SimpleNamespace(responses=responses)
    return OpenAIPlanningProvider(
        "not-a-real-key", "gpt-5.6-sol", 30, 12000, client=client
    ), responses


def successful_response(parsed=None):
    return SimpleNamespace(
        id="resp_123",
        model="gpt-5.6-sol",
        output_parsed=result_data() if parsed is None else parsed,
        output=[],
        usage=SimpleNamespace(input_tokens=100, output_tokens=200, total_tokens=300),
    )


def test_prompt_is_deterministic_complete_and_excludes_binary_content():
    first = build_planning_prompt(planning_request())
    second = build_planning_prompt(planning_request())
    assert first == second
    for value in (
        "MUSIC_VIDEO",
        "Silver Signal",
        "target_duration_seconds",
        "shot-001",
        "asset-1",
        "No logos or text.",
    ):
        assert value in first
    assert "must-not-be-sent" not in first


def test_success_uses_one_responses_parse_call_and_exposes_safe_evidence():
    provider, responses = fake_provider(successful_response())
    response_id = provider.submit_plan(planning_request())
    assert response_id == "resp_123"
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["model"] == "gpt-5.6-sol"
    assert call["text_format"].__name__ == "PlanningResult"
    assert call["reasoning"] == {"effort": "low"}
    assert call["extra_headers"] == {"Idempotency-Key": "idempotency-1"}
    assert "tools" not in call
    assert provider.get_status(response_id).status == NormalizedStatus.SUCCEEDED
    assert provider.get_result(response_id).concept_title == "Silver Signal"
    evidence = provider.get_evidence(response_id)
    assert evidence["provider_response_id"] == "resp_123"
    assert evidence["usage"] == {
        "input_tokens": 100,
        "output_tokens": 200,
        "total_tokens": 300,
    }


def test_refusal_and_malformed_results_fail_without_a_result():
    refusal = SimpleNamespace(
        id="resp_refused",
        output_parsed=None,
        output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])],
        usage=None,
    )
    provider, _ = fake_provider(refusal)
    with pytest.raises(OpenAIPlanningError, match="refused") as caught:
        provider.submit_plan(planning_request())
    assert caught.value.retryable is False

    provider, _ = fake_provider(successful_response({"schema_version": "music-video-plan-v1"}))
    with pytest.raises(ValidationError):
        provider.submit_plan(planning_request())


@pytest.mark.parametrize(
    ("error", "code", "retryable", "ambiguous"),
    [
        (
            openai.AuthenticationError(
                "bad secret",
                response=httpx.Response(
                    401, request=httpx.Request("POST", "https://api.openai.com")
                ),
                body=None,
            ),
            "OPENAI_AUTHENTICATION",
            False,
            False,
        ),
        (
            openai.RateLimitError(
                "slow down",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://api.openai.com")
                ),
                body=None,
            ),
            "OPENAI_RATE_LIMIT",
            True,
            False,
        ),
        (
            openai.RateLimitError(
                "quota exhausted",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://api.openai.com")
                ),
                body={"error": {"code": "insufficient_quota"}},
            ),
            "OPENAI_INSUFFICIENT_QUOTA",
            False,
            False,
        ),
        (
            openai.NotFoundError(
                "model unavailable",
                response=httpx.Response(
                    404, request=httpx.Request("POST", "https://api.openai.com")
                ),
                body={"error": {"code": "model_not_found"}},
            ),
            "OPENAI_MODEL_NOT_AVAILABLE",
            False,
            False,
        ),
        (
            openai.InternalServerError(
                "temporary",
                response=httpx.Response(
                    500, request=httpx.Request("POST", "https://api.openai.com")
                ),
                body=None,
            ),
            "OPENAI_SERVER_ERROR",
            True,
            False,
        ),
        (
            openai.APITimeoutError(httpx.Request("POST", "https://api.openai.com")),
            "OPENAI_AMBIGUOUS_TIMEOUT",
            False,
            True,
        ),
    ],
)
def test_error_mapping(error, code, retryable, ambiguous):
    provider, _ = fake_provider(error=error)
    with pytest.raises(OpenAIPlanningError) as caught:
        provider.submit_plan(planning_request())
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.ambiguous is ambiguous


def test_preconnect_failure_is_retryable_but_lost_connection_is_ambiguous():
    request = httpx.Request("POST", "https://api.openai.com")
    preconnect = openai.APIConnectionError(request=request)
    preconnect.__cause__ = httpx.ConnectError("dns", request=request)
    provider, _ = fake_provider(error=preconnect)
    with pytest.raises(OpenAIPlanningError) as caught:
        provider.submit_plan(planning_request())
    assert caught.value.retryable is True

    lost = openai.APIConnectionError(request=request)
    lost.__cause__ = ConnectionResetError("lost after send")
    provider, _ = fake_provider(error=lost)
    with pytest.raises(OpenAIPlanningError) as caught:
        provider.submit_plan(planning_request())
    assert caught.value.ambiguous is True
    assert caught.value.retryable is False


def test_usage_cost_configuration_and_secret_sanitization():
    settings.openai_planning_input_usd_per_million = 4
    settings.openai_planning_output_usd_per_million = 20
    assert estimate_tokens("12345") == 2
    assert estimate_cost(1_000_000, 1_000_000) == {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "estimated_input_cost_usd": 4.0,
        "estimated_output_cost_usd": 20.0,
        "estimated_total_cost_usd": 24.0,
        "actual_cost_usd": None,
    }
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, planning_provider="openai")
    configured = Settings(
        _env_file=None, planning_provider="openai", openai_api_key="sk-deliberate-secret"
    )
    assert "sk-deliberate-secret" not in repr(configured)
    settings.openai_api_key = SecretStr("sk-deliberate-secret")
    dirty = {
        "message": "Bearer token sk-deliberate-secret",
        "OPENAI_API_KEY": "sk-deliberate-secret",
        "x-api-key": "another-secret",
        "url": "https://example.test/path?api_key=query-secret&ok=yes",
        "nested_url_message": "request failed at https://example.test/path?token=nested-secret&ok=yes",
    }
    clean = sanitize(dirty)
    assert all(
        secret not in str(clean)
        for secret in (
            "sk-deliberate-secret",
            "another-secret",
            "query-secret",
            "nested-secret",
        )
    )
    assert REDACTED in str(clean)
