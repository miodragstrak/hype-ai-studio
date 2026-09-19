from backend.app.config import settings
from backend.app.providers.base import VideoProvider
from backend.app.providers.mock import MockVideoProvider
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
