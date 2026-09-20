from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PlanningRequest(BaseModel):
    project_id: str
    project_type: Literal["MUSIC_VIDEO"] = "MUSIC_VIDEO"
    project_title: str = "Untitled music video"
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


class PlanningResult(BaseModel):
    schema_version: Literal["music-video-plan-v1"]
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
        return self


def validate_plan(
    result: PlanningResult,
    *,
    target_duration: float,
    maximum_shots: int,
    allowed_asset_ids: set[str],
) -> PlanningResult:
    if len(result.shots) > maximum_shots:
        raise ValueError("plan exceeds maximum shot count")
    total = sum(shot.duration_seconds for shot in result.shots)
    tolerance = max(1.0, target_duration * 0.1)
    if abs(total - target_duration) > tolerance:
        raise ValueError("planned duration is outside the 10% target tolerance")
    referenced = {asset for shot in result.shots for asset in shot.reference_asset_ids}
    if not referenced.issubset(allowed_asset_ids):
        raise ValueError("plan references an asset outside this project")
    return result
