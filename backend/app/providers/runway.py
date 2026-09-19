from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from runwayml import RunwayML

from backend.app.domain.models import (
    GenerationRequest,
    GenerationResult,
    GenerationStatus,
    NormalizedError,
    NormalizedStatus,
    OutputReference,
)
from backend.app.security import sanitize

_STATUS_MAP = {
    "PENDING": NormalizedStatus.PENDING,
    "RUNNING": NormalizedStatus.PROCESSING,
    "THROTTLED": NormalizedStatus.PENDING,
    "SUCCEEDED": NormalizedStatus.SUCCEEDED,
    "FAILED": NormalizedStatus.FAILED,
    "CANCELED": NormalizedStatus.CANCELLED,
    "CANCELLED": NormalizedStatus.CANCELLED,
}


class ProviderValidationError(ValueError):
    retryable = False
    code = "PROVIDER_VALIDATION_ERROR"


class AmbiguousProviderSubmissionError(RuntimeError):
    retryable = False
    code = "AMBIGUOUS_PROVIDER_SUBMISSION"


class DownloadError(RuntimeError):
    retryable = True
    code = "PROVIDER_OUTPUT_DOWNLOAD_FAILED"


class RunwayVideoProvider:
    provider = "runway"

    def __init__(
        self,
        output_root: Path,
        api_key: str,
        model: Literal["gen4.5"] = "gen4.5",
        download_timeout_seconds: float = 120,
        download_max_bytes: int = 250 * 1024 * 1024,
        client: RunwayML | Any | None = None,
        http_client: httpx.Client | Any | None = None,
    ):
        if not api_key:
            raise ProviderValidationError("Runway API secret is required")
        self.output_root = output_root
        self.model = model
        self.download_timeout_seconds = download_timeout_seconds
        self.download_max_bytes = download_max_bytes
        self.client = client or RunwayML(api_key=api_key, max_retries=0)
        self.http_client = http_client

    def submit_generation(self, request: GenerationRequest) -> str:
        duration = int(request.target_duration)
        if duration != request.target_duration or not 2 <= duration <= 10:
            raise ProviderValidationError(
                "Runway Gen-4.5 duration must be a whole number from 2 to 10 seconds"
            )
        ratio = cast(
            Literal["1280:720", "720:1280"],
            {"16:9": "1280:720", "9:16": "720:1280"}.get(request.aspect_ratio),
        )
        if ratio is None:
            raise ProviderValidationError("Runway Gen-4.5 supports 16:9 or 9:16")
        try:
            task = self.client.text_to_video.create(
                model=self.model,
                prompt_text=request.prompt,
                ratio=ratio,
                duration=duration,
                output_format="mp4",
            )
        except Exception as exc:
            raise AmbiguousProviderSubmissionError(
                "Runway submission outcome is unknown; manual reconciliation is required"
            ) from exc
        return task.id

    def get_status(self, provider_job_id: str) -> GenerationStatus:
        task = self.client.tasks.retrieve(provider_job_id)
        status = _STATUS_MAP.get(task.status, NormalizedStatus.PROCESSING)
        error = None
        if status == NormalizedStatus.FAILED:
            failure = sanitize(getattr(task, "failure", None) or {})
            error = NormalizedError(
                "RUNWAY_TASK_FAILED", "Runway generation failed", details={"failure": failure}
            )
        return GenerationStatus(
            provider_job_id,
            status,
            sanitize({"provider": self.provider, "model": self.model}),
            error,
        )

    def get_result(self, provider_job_id: str) -> GenerationResult:
        task = self.client.tasks.retrieve(provider_job_id)
        status = _STATUS_MAP.get(task.status, NormalizedStatus.PROCESSING)
        if status != NormalizedStatus.SUCCEEDED:
            return GenerationResult(provider_job_id, status)
        output_urls = cast(Any, task).output
        if not output_urls:
            return GenerationResult(
                provider_job_id,
                NormalizedStatus.FAILED,
                error=NormalizedError("RUNWAY_OUTPUT_MISSING", "Runway returned no video output"),
            )
        output = self.output_root / f"{provider_job_id}.mp4"
        self._download(output_urls[0], output)
        return GenerationResult(
            provider_job_id,
            NormalizedStatus.SUCCEEDED,
            [OutputReference(str(output))],
            sanitize({"model": self.model, "task_status": task.status}),
        )

    def _download(self, url: str, destination: Path) -> None:
        if urlsplit(url).scheme != "https":
            raise DownloadError("Runway output URL must use HTTPS")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".partial")
        client = self.http_client or httpx.Client(
            timeout=httpx.Timeout(self.download_timeout_seconds),
            follow_redirects=True,
            max_redirects=3,
        )
        close_client = self.http_client is None
        try:
            total = 0
            with client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.download_max_bytes:
                    raise DownloadError("Runway output exceeds configured size limit")
                with partial.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > self.download_max_bytes:
                            raise DownloadError("Runway output exceeds configured size limit")
                        output.write(chunk)
            if total == 0:
                raise DownloadError("Runway output was empty")
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        finally:
            if close_client:
                client.close()

    def cancel(self, provider_job_id: str) -> GenerationStatus:
        self.client.tasks.delete(provider_job_id)
        return GenerationStatus(provider_job_id, NormalizedStatus.CANCELLED)
