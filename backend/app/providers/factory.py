from backend.app.config import settings
from backend.app.providers.base import VideoProvider
from backend.app.providers.mock import MockVideoProvider
from backend.app.providers.mock_planning import MockPlanningProvider
from backend.app.providers.openai_planning import OpenAIPlanningProvider
from backend.app.providers.planning import PlanningProvider
from backend.app.providers.runway import RunwayVideoProvider


def create_video_provider(failure_mode: str = "none") -> VideoProvider:
    if settings.video_provider == "mock":
        return MockVideoProvider(
            settings.storage_root / "mock-provider",
            settings.mock_provider_delay_seconds,
            failure_mode,
        )
    if settings.video_provider == "runway":
        if not settings.runwayml_api_secret:
            raise ValueError("RUNWAYML_API_SECRET is required when VIDEO_PROVIDER=runway")
        return RunwayVideoProvider(
            settings.storage_root / "runway-provider",
            settings.runwayml_api_secret.get_secret_value(),
            "gen4.5",
            settings.runway_download_timeout_seconds,
            settings.runway_download_max_bytes,
        )
    raise ValueError(f"Unsupported video provider: {settings.video_provider}")


def create_planning_provider(failure_mode: str = "none") -> PlanningProvider:
    if settings.planning_provider == "mock":
        return MockPlanningProvider(settings.mock_planning_delay_seconds, failure_mode)
    if settings.planning_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when PLANNING_PROVIDER=openai")
        return OpenAIPlanningProvider(
            settings.openai_api_key.get_secret_value(),
            settings.openai_planning_model,
            settings.openai_planning_timeout_seconds,
            settings.openai_planning_max_output_tokens,
        )
    raise ValueError(f"Unsupported planning provider: {settings.planning_provider}")
