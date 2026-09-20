from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://hype:hype@localhost:5432/hype"
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "hype:jobs"
    storage_root: Path = Path(".data/storage")
    max_asset_upload_bytes: int = 25 * 1024 * 1024
    mock_provider_delay_seconds: float = 0.1
    mock_provider_failure_mode: str = "none"
    planning_provider: str = "mock"
    openai_api_key: SecretStr | None = None
    openai_planning_model: str = "gpt-5.6-sol"
    openai_planning_timeout_seconds: float = 120
    openai_planning_max_output_tokens: int = 12_000
    openai_planning_input_usd_per_million: float = 4
    openai_planning_output_usd_per_million: float = 20
    openai_planning_soft_limit_usd: float = 2
    openai_planning_hard_limit_usd: float = 5
    mock_planning_delay_seconds: float = 0.1
    mock_planning_failure_mode: str = "none"
    planning_max_shot_count: int = 24
    planning_prompt_schema_version: str = "music-video-plan-v1"
    video_provider: str = "mock"
    runwayml_api_secret: SecretStr | None = None
    runway_video_model: str = "gen4.5"
    runway_video_duration_seconds: int = 5
    runway_video_ratio: str = "1280:720"
    runway_credit_usd_rate: float = 0.01
    runway_model_credits_per_second: float = 12
    runway_soft_limit_usd: float = 10
    runway_hard_limit_usd: float = 30
    runway_poll_interval_seconds: float = 5
    runway_task_timeout_seconds: float = 900
    runway_download_timeout_seconds: float = 120
    runway_download_max_bytes: int = 250 * 1024 * 1024
    max_retries: int = 2
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174"
    )
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_runway(self):
        if self.video_provider not in {"mock", "runway"}:
            raise ValueError("VIDEO_PROVIDER must be mock or runway")
        if self.video_provider == "runway" and not self.runwayml_api_secret:
            raise ValueError("RUNWAYML_API_SECRET is required when VIDEO_PROVIDER=runway")
        if self.runway_video_model != "gen4.5":
            raise ValueError("RUNWAY_VIDEO_MODEL must be gen4.5")
        if not 2 <= self.runway_video_duration_seconds <= 10:
            raise ValueError("RUNWAY_VIDEO_DURATION_SECONDS must be between 2 and 10")
        if self.runway_video_ratio != "1280:720":
            raise ValueError("RUNWAY_VIDEO_RATIO must be 1280:720")
        positive = {
            "RUNWAY_CREDIT_USD_RATE": self.runway_credit_usd_rate,
            "RUNWAY_MODEL_CREDITS_PER_SECOND": self.runway_model_credits_per_second,
            "RUNWAY_SOFT_LIMIT_USD": self.runway_soft_limit_usd,
            "RUNWAY_HARD_LIMIT_USD": self.runway_hard_limit_usd,
            "RUNWAY_POLL_INTERVAL_SECONDS": self.runway_poll_interval_seconds,
            "RUNWAY_TASK_TIMEOUT_SECONDS": self.runway_task_timeout_seconds,
            "RUNWAY_DOWNLOAD_TIMEOUT_SECONDS": self.runway_download_timeout_seconds,
            "RUNWAY_DOWNLOAD_MAX_BYTES": self.runway_download_max_bytes,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.runway_hard_limit_usd < self.runway_soft_limit_usd:
            raise ValueError("RUNWAY_HARD_LIMIT_USD must be at least RUNWAY_SOFT_LIMIT_USD")
        if self.planning_provider not in {"mock", "openai"}:
            raise ValueError("PLANNING_PROVIDER must be mock or openai")
        if self.planning_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when PLANNING_PROVIDER=openai")
        if self.openai_planning_model != "gpt-5.6-sol":
            raise ValueError("OPENAI_PLANNING_MODEL must be gpt-5.6-sol")
        planning_positive = {
            "OPENAI_PLANNING_TIMEOUT_SECONDS": self.openai_planning_timeout_seconds,
            "OPENAI_PLANNING_MAX_OUTPUT_TOKENS": self.openai_planning_max_output_tokens,
            "OPENAI_PLANNING_INPUT_USD_PER_MILLION": self.openai_planning_input_usd_per_million,
            "OPENAI_PLANNING_OUTPUT_USD_PER_MILLION": self.openai_planning_output_usd_per_million,
            "OPENAI_PLANNING_SOFT_LIMIT_USD": self.openai_planning_soft_limit_usd,
            "OPENAI_PLANNING_HARD_LIMIT_USD": self.openai_planning_hard_limit_usd,
        }
        for name, value in planning_positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.openai_planning_hard_limit_usd < self.openai_planning_soft_limit_usd:
            raise ValueError("OPENAI_PLANNING_HARD_LIMIT_USD must be at least the soft limit")
        if self.mock_planning_delay_seconds < 0:
            raise ValueError("MOCK_PLANNING_DELAY_SECONDS must not be negative")
        if not 1 <= self.planning_max_shot_count <= 48:
            raise ValueError("PLANNING_MAX_SHOT_COUNT must be between 1 and 48")
        return self


settings = Settings()
