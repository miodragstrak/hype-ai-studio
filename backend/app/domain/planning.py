from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PlanningRequest(BaseModel):
    project_id: str
    project_type: Literal["MUSIC_VIDEO", "TOUR_GUIDE"] = "MUSIC_VIDEO"
    project_title: str = "Untitled project"
    creative_brief: str = Field(min_length=1)
    target_duration_seconds: float = Field(gt=0, le=600)
    aspect_ratio: str
    visual_tone: str = Field(min_length=1)
    narrative_approach: str = Field(min_length=1)
    performance_presence: str = Field(min_length=1)
    pacing: str = Field(min_length=1)
    constraints: str = ""
    reference_assets: list[dict[str, Any]] = Field(default_factory=list)
    maximum_shot_count: int = Field(ge=1, le=48)
    correlation_id: str
    idempotency_key: str


class CreativeDirection(BaseModel):
    visual_style: str = Field(min_length=1)
    color_palette: list[str] = Field(min_length=1)
    camera_language: str = Field(min_length=1)
    editing_rhythm: str = Field(min_length=1)
    performance_direction: str = Field(min_length=1)
    continuity_notes: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class PlanShot(BaseModel):
    item_key: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0, le=60)
    shot_type: str = Field(min_length=1)
    camera: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    continuity_notes: str = ""
    reference_asset_ids: list[str] = Field(default_factory=list)
    location_or_motif: str | None = None
    pov_description: str | None = None
    narration: str | None = None
    factual_claims: list["FactualClaim"] = Field(default_factory=list)


class FactualClaim(BaseModel):
    claim: str = Field(min_length=1, max_length=500)
    sources: list[str] = Field(default_factory=list)


class PlanningResult(BaseModel):
    schema_version: Literal["music-video-plan-v1", "tour-guide-plan-v1"]
    concept_title: str = Field(min_length=1)
    logline: str = Field(min_length=1)
    treatment: str = Field(min_length=1)
    creative_direction: CreativeDirection
    shots: list[PlanShot] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_and_unique(self):
        ordinals = [shot.ordinal for shot in self.shots]
        if ordinals != list(range(1, len(self.shots) + 1)):
            raise ValueError("shot ordinals must be unique, consecutive, and ordered")
        keys = [shot.item_key for shot in self.shots]
        if len(keys) != len(set(keys)):
            raise ValueError("shot item keys must be unique")
        if self.schema_version == "tour-guide-plan-v1":
            if not 4 <= len(self.shots) <= 5:
                raise ValueError("tour guide plan must contain 4 or 5 scenes")
            for scene in self.shots:
                if not all(
                    value and value.strip()
                    for value in (scene.location_or_motif, scene.pov_description, scene.narration)
                ):
                    raise ValueError("tour guide scenes require location, POV description, and narration")
        return self


def validate_plan(
    result: PlanningResult,
    *,
    target_duration: float,
    maximum_shots: int,
    allowed_asset_ids: set[str],
    require_verified_sources: bool = True,
) -> PlanningResult:
    if len(result.shots) > maximum_shots:
        raise ValueError("plan exceeds maximum shot count")
    if result.schema_version == "tour-guide-plan-v1":
        if not 30 <= target_duration <= 45:
            raise ValueError("tour guide target duration must be between 30 and 45 seconds")
        missing = [
            claim.claim
            for scene in result.shots
            for claim in scene.factual_claims
            if not claim.sources
        ]
        if missing and require_verified_sources:
            raise ValueError("tour guide plan contains factual claims without sources")
        invalid_sources = [
            source
            for scene in result.shots
            for claim in scene.factual_claims
            for source in claim.sources
            if not source.startswith(("https://", "http://"))
        ]
        if invalid_sources:
            raise ValueError("tour guide fact sources must be HTTP(S) URLs")
    total = sum(shot.duration_seconds for shot in result.shots)
    tolerance = max(1.0, target_duration * 0.1)
    if abs(total - target_duration) > tolerance:
        raise ValueError("planned duration is outside the 10% target tolerance")
    referenced = {asset for shot in result.shots for asset in shot.reference_asset_ids}
    if not referenced.issubset(allowed_asset_ids):
        raise ValueError("plan references an asset outside this project")
    return result
