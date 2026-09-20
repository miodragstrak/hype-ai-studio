import math
import time
from uuid import uuid4

from backend.app.domain.models import GenerationStatus, NormalizedError, NormalizedStatus
from backend.app.domain.planning import CreativeDirection, PlanningRequest, PlanningResult, PlanShot


class MockPlanningProvider:
    provider = "mock"
    model = "mock-music-video-planner-v1"

    def __init__(self, delay_seconds: float = 0.1, failure_mode: str = "none"):
        self.delay_seconds = delay_seconds
        self.failure_mode = failure_mode
        self.jobs: dict[str, dict] = {}

    def submit_plan(self, request: PlanningRequest) -> str:
        job_id = f"mock-plan-{uuid4()}"
        self.jobs[job_id] = {"request": request, "created": time.monotonic(), "cancelled": False}
        return job_id

    def get_status(self, provider_job_id: str) -> GenerationStatus:
        job = self.jobs[provider_job_id]
        if job["cancelled"]:
            status = NormalizedStatus.CANCELLED
        elif self.failure_mode in {"transient", "permanent", "malformed"}:
            status = NormalizedStatus.FAILED if self.failure_mode != "malformed" else NormalizedStatus.SUCCEEDED
        elif time.monotonic() - job["created"] < self.delay_seconds:
            status = NormalizedStatus.PENDING
        else:
            status = NormalizedStatus.SUCCEEDED
        error = None
        if status == NormalizedStatus.FAILED:
            error = NormalizedError("MOCK_PLANNING_FAILURE", "controlled planning failure", self.failure_mode == "transient")
        return GenerationStatus(provider_job_id, status, {"provider": self.provider}, error)

    def get_result(self, provider_job_id: str) -> PlanningResult:
        request: PlanningRequest = self.jobs[provider_job_id]["request"]
        if self.failure_mode == "malformed":
            return PlanningResult.model_validate({"schema_version": "music-video-plan-v1"})
        count = min(request.maximum_shot_count, max(2, math.ceil(request.target_duration_seconds / 5)))
        base = round(request.target_duration_seconds / count, 2)
        durations = [base] * count
        durations[-1] = round(request.target_duration_seconds - sum(durations[:-1]), 2)
        refs = [str(asset["id"]) for asset in request.reference_assets[:1]]
        shots = [
            PlanShot(
                item_key=f"shot-{index:03d}", ordinal=index,
                title=f"{request.visual_tone} movement {index}",
                description=f"Beat {index} develops the {request.narrative_approach} approach.",
                prompt=f"{request.creative_brief}. {request.visual_tone}; {request.pacing} pacing; cinematic 16:9 shot {index}.",
                duration_seconds=durations[index - 1], shot_type="wide" if index == 1 else "medium",
                camera="slow controlled movement", subject=request.performance_presence,
                environment=request.visual_tone, continuity_notes="Maintain palette and screen direction.",
                reference_asset_ids=refs,
            )
            for index in range(1, count + 1)
        ]
        return PlanningResult(
            schema_version="music-video-plan-v1",
            concept_title=f"{request.visual_tone.title()} — {request.narrative_approach.title()}",
            logline=f"A {request.pacing} music video shaped by {request.creative_brief}",
            treatment=f"The film translates the brief into a {request.visual_tone} visual progression with {request.performance_presence}. {request.constraints}".strip(),
            creative_direction=CreativeDirection(
                visual_style=request.visual_tone, color_palette=["silver", "midnight blue", "soft white"],
                camera_language="Measured wides with purposeful forward movement",
                editing_rhythm=f"{request.pacing} cuts that follow the planned arc",
                performance_direction=request.performance_presence,
                continuity_notes=["Preserve palette", "Maintain directional continuity"],
                avoid=["logos", "on-screen text"],
            ), shots=shots,
        )

    def cancel(self, provider_job_id: str) -> GenerationStatus:
        self.jobs[provider_job_id]["cancelled"] = True
        return self.get_status(provider_job_id)
