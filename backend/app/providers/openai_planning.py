from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import openai
from openai import OpenAI

from backend.app.domain.models import GenerationStatus, NormalizedStatus
from backend.app.domain.planning import PlanningRequest, PlanningResult


class OpenAIPlanningError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False, ambiguous: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous


def build_planning_prompt(request: PlanningRequest) -> str:
    asset_summaries = [
        {
            "id": asset.get("id"),
            "asset_type": asset.get("asset_type"),
            "mime_type": asset.get("mime_type"),
            "size_bytes": asset.get("size_bytes"),
            "rights_metadata": asset.get("rights_metadata", {}),
        }
        for asset in request.reference_assets
    ]
    inputs = {
        "project_type": request.project_type,
        "project_title": request.project_title,
        "creative_brief": request.creative_brief,
        "target_duration_seconds": request.target_duration_seconds,
        "aspect_ratio": request.aspect_ratio,
        "visual_tone": request.visual_tone,
        "narrative_approach": request.narrative_approach,
        "performance_presence": request.performance_presence,
        "pacing": request.pacing,
        "constraints": request.constraints,
        "maximum_shot_count": request.maximum_shot_count,
        "reference_assets": asset_summaries,
    }
    return (
        "Create a production-ready music-video treatment and ordered shot plan from the "
        "JSON inputs below. Return only the requested structured result. Use consecutive "
        "ordinals starting at 1 and stable item keys shot-001, shot-002, and so on. Include "
        "a concrete description and video-generation prompt for every shot. Keep every "
        "duration positive, use no more than maximum_shot_count shots, and make the total "
        "duration match target_duration_seconds within 10%. Reference only supplied asset IDs. "
        "Do not use web search or infer facts about real people.\n\n"
        + json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    )


class OpenAIPlanningProvider:
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        *,
        client: Any | None = None,
    ):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.client = client or OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._results: dict[str, PlanningResult] = {}
        self._evidence: dict[str, dict[str, Any]] = {}

    def submit_plan(self, request: PlanningRequest) -> str:
        started_at = datetime.now(UTC)
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a music-video planning system. Follow the supplied constraints "
                            "and produce the complete structured plan without calling tools."
                        ),
                    },
                    {"role": "user", "content": build_planning_prompt(request)},
                ],
                text_format=PlanningResult,
                reasoning={"effort": "low"},
                max_output_tokens=self.max_output_tokens,
                extra_headers={"Idempotency-Key": request.idempotency_key},
            )
        except Exception as exc:
            raise self._map_error(exc) from exc
        completed_at = datetime.now(UTC)
        response_id = getattr(response, "id", None)
        if not response_id:
            raise OpenAIPlanningError(
                "OPENAI_MISSING_RESPONSE_ID",
                "OpenAI returned no response ID; manual reconciliation is required",
                ambiguous=True,
            )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            refused = any(
                getattr(content, "type", None) == "refusal"
                for output in getattr(response, "output", [])
                for content in getattr(output, "content", [])
            )
            code = "OPENAI_REFUSAL" if refused else "OPENAI_UNPARSEABLE_RESULT"
            message = (
                "OpenAI refused the planning request"
                if refused
                else "OpenAI returned no parsed plan"
            )
            raise OpenAIPlanningError(code, message)
        result = PlanningResult.model_validate(parsed)
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0)
        self._results[response_id] = result
        self._evidence[response_id] = {
            "provider_response_id": response_id,
            "model": getattr(response, "model", None) or self.model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "request_started_at": started_at.isoformat(),
            "request_completed_at": completed_at.isoformat(),
        }
        return response_id

    def get_status(self, provider_job_id: str) -> GenerationStatus:
        status = (
            NormalizedStatus.SUCCEEDED
            if provider_job_id in self._results
            else NormalizedStatus.FAILED
        )
        return GenerationStatus(provider_job_id, status, {"provider": self.provider})

    def get_result(self, provider_job_id: str) -> PlanningResult:
        try:
            return self._results[provider_job_id]
        except KeyError as exc:
            raise OpenAIPlanningError(
                "OPENAI_RESULT_NOT_AVAILABLE", "OpenAI result is unavailable"
            ) from exc

    def get_evidence(self, provider_job_id: str) -> dict[str, Any]:
        return dict(self._evidence[provider_job_id])

    def cancel(self, provider_job_id: str) -> GenerationStatus:
        return GenerationStatus(
            provider_job_id, NormalizedStatus.CANCELLED, {"provider": self.provider}
        )

    @staticmethod
    def _map_error(exc: Exception) -> OpenAIPlanningError:
        if isinstance(exc, openai.APITimeoutError):
            return OpenAIPlanningError(
                "OPENAI_AMBIGUOUS_TIMEOUT",
                "OpenAI submission timed out; manual reconciliation is required",
                ambiguous=True,
            )
        if isinstance(exc, openai.APIConnectionError):
            if isinstance(exc.__cause__, httpx.ConnectError):
                return OpenAIPlanningError(
                    "OPENAI_CONNECT_FAILED",
                    "OpenAI connection failed before submission",
                    retryable=True,
                )
            return OpenAIPlanningError(
                "OPENAI_AMBIGUOUS_CONNECTION",
                "OpenAI connection was lost; manual reconciliation is required",
                ambiguous=True,
            )
        if isinstance(exc, openai.RateLimitError):
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
            if (
                body.get("code") == "insufficient_quota"
                or error.get("code") == "insufficient_quota"
            ):
                return OpenAIPlanningError(
                    "OPENAI_INSUFFICIENT_QUOTA", "OpenAI account quota is insufficient"
                )
            return OpenAIPlanningError(
                "OPENAI_RATE_LIMIT", "OpenAI rate limit reached", retryable=True
            )
        if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
            return OpenAIPlanningError(
                "OPENAI_SERVER_ERROR", "OpenAI temporary server failure", retryable=True
            )
        if isinstance(exc, openai.AuthenticationError):
            return OpenAIPlanningError("OPENAI_AUTHENTICATION", "OpenAI credentials were rejected")
        if isinstance(exc, openai.PermissionDeniedError):
            return OpenAIPlanningError("OPENAI_PERMISSION", "OpenAI access was denied")
        if isinstance(exc, openai.NotFoundError):
            return OpenAIPlanningError(
                "OPENAI_MODEL_NOT_AVAILABLE", "The configured OpenAI model is unavailable"
            )
        if isinstance(exc, openai.BadRequestError):
            return OpenAIPlanningError(
                "OPENAI_INVALID_REQUEST", "OpenAI rejected the planning request"
            )
        return OpenAIPlanningError(
            "OPENAI_SUBMISSION_AMBIGUOUS",
            "OpenAI submission outcome is unknown; manual reconciliation is required",
            ambiguous=True,
        )
