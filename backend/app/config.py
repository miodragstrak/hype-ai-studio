from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://hype:hype@localhost:5432/hype"
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "hype:jobs"
    storage_root: Path = Path(".data/storage")
    mock_provider_delay_seconds: float = 0.1
    mock_provider_failure_mode: str = "none"
    max_retries: int = 2
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174"
    )
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
