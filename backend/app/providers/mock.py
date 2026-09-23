import subprocess
import time
from pathlib import Path
from uuid import uuid4

from backend.app.config import settings
from backend.app.domain.models import (
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
    NormalizedError,
    NormalizedStatus,
    OutputReference,
)


class MockVideoProvider:
    provider = "mock"
    model = "mock-video-v1"

    def __init__(self, output_root: Path, delay_seconds: float = 0.1, failure_mode: str = "none"):
        self.output_root = output_root
        self.delay_seconds = delay_seconds
        self.failure_mode = failure_mode
        self.jobs: dict[str, dict] = {}

    def submit_generation(self, request: GenerationRequest) -> str:
        provider_job_id = f"mock-{uuid4()}"
        self.jobs[provider_job_id] = {
            "request": request,
            "created": time.monotonic(),
            "cancelled": False,
        }
        return provider_job_id

    def get_status(self, provider_job_id: str) -> GenerationStatus:
        job = self.jobs[provider_job_id]
        if job["cancelled"]:
            status = NormalizedStatus.CANCELLED
        elif self.failure_mode in {"permanent", "transient"}:
            status = NormalizedStatus.FAILED
        elif time.monotonic() - job["created"] < self.delay_seconds:
            status = NormalizedStatus.PENDING
        else:
            status = NormalizedStatus.SUCCEEDED
        error = (
            NormalizedError(
                "MOCK_FAILURE",
                "controlled mock failure",
                self.failure_mode == "transient",
            )
            if status == NormalizedStatus.FAILED
            else None
        )
        return GenerationStatus(provider_job_id, status, {"provider": self.provider}, error)

    def get_result(self, provider_job_id: str) -> GenerationResult:
        status = self.get_status(provider_job_id)
        if status.status != NormalizedStatus.SUCCEEDED:
            return GenerationResult(provider_job_id, status.status, error=status.error)
        request = self.jobs[provider_job_id]["request"]
        resolution = "180x320" if request.aspect_ratio == "9:16" else "320x180"
        self.output_root.mkdir(parents=True, exist_ok=True)
        output = self.output_root / f"{provider_job_id}.mp4"
        command = [
            settings.ffmpeg_executable,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x17324d:s={resolution}:d={request.target_duration}",
            "-vf",
            "drawtext=text='HYPE MOCK':fontcolor=white:fontsize=24:x=20:y=80",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode:
            return GenerationResult(
                provider_job_id,
                NormalizedStatus.FAILED,
                error=NormalizedError("FFMPEG_FAILED", completed.stderr[-500:], True),
            )
        return GenerationResult(
            provider_job_id,
            NormalizedStatus.SUCCEEDED,
            [OutputReference(str(output))],
            {"model": self.model},
            {"duration": request.target_duration},
        )

    def cancel(self, provider_job_id: str) -> GenerationStatus:
        self.jobs[provider_job_id]["cancelled"] = True
        return self.get_status(provider_job_id)
