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
        self.model = (
            "mock-tour-guide-planner-v1"
            if request.project_type == "TOUR_GUIDE"
            else "mock-music-video-planner-v1"
        )
        job_id = f"mock-plan-{uuid4()}"
        self.jobs[job_id] = {"request": request, "created": time.monotonic(), "cancelled": False}
        return job_id

    def get_status(self, provider_job_id: str) -> GenerationStatus:
        job = self.jobs[provider_job_id]
        if job["cancelled"]:
            status = NormalizedStatus.CANCELLED
        elif self.failure_mode in {"transient", "permanent", "malformed"}:
            status = (
                NormalizedStatus.FAILED
                if self.failure_mode != "malformed"
                else NormalizedStatus.SUCCEEDED
            )
        elif time.monotonic() - job["created"] < self.delay_seconds:
            status = NormalizedStatus.PENDING
        else:
            status = NormalizedStatus.SUCCEEDED
        error = None
        if status == NormalizedStatus.FAILED:
            error = NormalizedError(
                "MOCK_PLANNING_FAILURE",
                "controlled planning failure",
                self.failure_mode == "transient",
            )
        return GenerationStatus(provider_job_id, status, {"provider": self.provider}, error)

    def get_result(self, provider_job_id: str) -> PlanningResult:
        request: PlanningRequest = self.jobs[provider_job_id]["request"]
        if self.failure_mode == "malformed":
            return PlanningResult.model_validate({"schema_version": "music-video-plan-v1"})
        if request.project_type == "TOUR_GUIDE":
            return self._tour_guide_result(request)
        count = min(
            request.maximum_shot_count, max(2, math.ceil(request.target_duration_seconds / 5))
        )
        base = round(request.target_duration_seconds / count, 2)
        durations = [base] * count
        durations[-1] = round(request.target_duration_seconds - sum(durations[:-1]), 2)
        refs = [str(asset["id"]) for asset in request.reference_assets[:1]]
        shots = [
            PlanShot(
                item_key=f"shot-{index:03d}",
                ordinal=index,
                title=f"{request.visual_tone} movement {index}",
                description=f"Beat {index} develops the {request.narrative_approach} approach.",
                prompt=f"{request.creative_brief}. {request.visual_tone}; {request.pacing} pacing; cinematic 16:9 shot {index}.",
                duration_seconds=durations[index - 1],
                shot_type="wide" if index == 1 else "medium",
                camera="slow controlled movement",
                subject=request.performance_presence,
                environment=request.visual_tone,
                continuity_notes="Maintain palette and screen direction.",
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
                visual_style=request.visual_tone,
                color_palette=["silver", "midnight blue", "soft white"],
                camera_language="Measured wides with purposeful forward movement",
                editing_rhythm=f"{request.pacing} cuts that follow the planned arc",
                performance_direction=request.performance_presence,
                continuity_notes=["Preserve palette", "Maintain directional continuity"],
                avoid=["logos", "on-screen text"],
            ),
            shots=shots,
        )

    def _tour_guide_result(self, request: PlanningRequest) -> PlanningResult:
        count = min(5, max(4, request.maximum_shot_count))
        base = round(request.target_duration_seconds / count, 2)
        durations = [base] * count
        durations[-1] = round(request.target_duration_seconds - sum(durations[:-1]), 2)
        locations = ["Uvodni naslov", "Kalemegdan", "Knez Mihailova"]
        if count == 5:
            locations.append("Skadarlija")
        locations.append("Završni kadar")
        scenes = []
        for index in range(1, count + 1):
            location = locations[index - 1]
            claims = []
            if location == "Kalemegdan":
                claims = [
                    {
                        "claim": "Kalemegdan pruža pogled na ušće Save u Dunav.",
                        "sources": [],
                    }
                ]
            narration = {
                "Uvodni naslov": "Beograd iz prvog lica, u nekoliko brzih stanica.",
                "Kalemegdan": "Počinjemo iznad reka, uz kadar koji traži proveru činjenice.",
                "Knez Mihailova": "Nastavljamo kroz ritam centralne pešačke ulice.",
                "Skadarlija": "Kratak prolaz kroz teksture stare gradske četvrti.",
                "Završni kadar": "Grad ostaje iza nas, a putopis se završava jednim pogledom.",
            }[location]
            scenes.append(
                PlanShot(
                    item_key=f"scene-{index:03d}",
                    ordinal=index,
                    title=location,
                    description=f"POV scena za motiv: {location}.",
                    prompt=f"Vertical 9:16 POV travel footage in Belgrade, {location}.",
                    duration_seconds=durations[index - 1],
                    shot_type="POV",
                    camera="handheld forward movement",
                    subject="Belgrade travel experience",
                    environment=location,
                    continuity_notes="Keep movement natural and narration concise.",
                    location_or_motif=location,
                    pov_description=f"Vertikalni POV prolazak kroz motiv {location}.",
                    narration=narration,
                    factual_claims=claims,
                )
            )
        return PlanningResult(
            schema_version="tour-guide-plan-v1",
            concept_title="Beograd iz prvog lica",
            logline="Kratak vertikalni POV putopis kroz prepoznatljive motive Beograda.",
            treatment=(
                "Mock predlog na srpskom povezuje uvodni naslov, gradske scene i završni kadar. "
                "Tvrdnje bez izvora moraju biti proverene pre odobrenja."
            ),
            creative_direction=CreativeDirection(
                visual_style="autentičan vertikalni video putopis",
                color_palette=["kamen", "zelenilo", "gradska svetla"],
                camera_language="POV kretanje u formatu 9:16",
                editing_rhythm="kratke scene sa jasnim prelazima",
                performance_direction="naracija na srpskom, bez voditelja u kadru",
                continuity_notes=["Dosledan POV", "Kratka naracija po sceni"],
                avoid=["neproverene činjenice", "izmišljeni izvori"],
            ),
            shots=scenes,
        )

    def cancel(self, provider_job_id: str) -> GenerationStatus:
        self.jobs[provider_job_id]["cancelled"] = True
        return self.get_status(provider_job_id)
