from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.config import Settings, settings
from backend.app.domain.models import GenerationRequest, NormalizedStatus
from backend.app.providers.factory import create_video_provider
from backend.app.providers.mock import MockVideoProvider
from backend.app.providers.runway import (
    AmbiguousProviderSubmissionError,
    DownloadError,
    RunwayVideoProvider,
)
from backend.app.security import REDACTED, sanitize
from backend.app.services.runway_budget import estimate_cost


class FakeCreate:
    def __init__(self, error=None):
        self.arguments = None
        self.calls = 0
        self.error = error

    def create(self, **kwargs):
        self.calls += 1
        self.arguments = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(id="runway-task-1")


class FakeTasks:
    def __init__(self, status="PENDING", output=None):
        self.status = status
        self.output = output or ["https://media.example/generated.mp4"]
        self.deleted = None

    def retrieve(self, task_id):
        return SimpleNamespace(status=self.status, output=self.output, failure=None)

    def delete(self, task_id):
        self.deleted = task_id


def make_provider(tmp_path: Path, create=None, tasks=None, http_client=None, max_bytes=1024):
    create = create or FakeCreate()
    tasks = tasks or FakeTasks()
    client = SimpleNamespace(text_to_video=create, tasks=tasks)
    return (
        RunwayVideoProvider(
            tmp_path,
            "test-secret",
            client=client,
            http_client=http_client,
            download_max_bytes=max_bytes,
        ),
        create,
        tasks,
    )


def test_factory_defaults_to_mock_and_runway_requires_secret(tmp_path):
    settings.video_provider = "mock"
    assert isinstance(create_video_provider(), MockVideoProvider)
    settings.video_provider = "runway"
    settings.runwayml_api_secret = None
    with pytest.raises(ValueError, match="RUNWAYML_API_SECRET"):
        create_video_provider()


def test_configuration_validation_and_secret_repr():
    with pytest.raises(ValidationError, match="RUNWAY_HARD_LIMIT_USD"):
        Settings(_env_file=None, runway_soft_limit_usd=10, runway_hard_limit_usd=9)
    with pytest.raises(ValidationError, match="RUNWAY_VIDEO_DURATION_SECONDS"):
        Settings(_env_file=None, runway_video_duration_seconds=11)
    with pytest.raises(ValidationError, match="RUNWAYML_API_SECRET"):
        Settings(_env_file=None, video_provider="runway")
    configured = Settings(
        _env_file=None, video_provider="runway", runwayml_api_secret="deliberate-secret"
    )
    assert "deliberate-secret" not in repr(configured)


def test_gen45_submit_is_immediate_and_normalized(tmp_path):
    provider, create, _ = make_provider(tmp_path)
    task_id = provider.submit_generation(GenerationRequest("A cinematic sunrise", "16:9", 5))
    assert task_id == "runway-task-1"
    assert create.calls == 1
    assert create.arguments == {
        "model": "gen4.5",
        "prompt_text": "A cinematic sunrise",
        "ratio": "1280:720",
        "duration": 5,
        "output_format": "mp4",
    }


@pytest.mark.parametrize(
    ("provider_status", "normalized"),
    [
        ("PENDING", NormalizedStatus.PENDING),
        ("THROTTLED", NormalizedStatus.PENDING),
        ("RUNNING", NormalizedStatus.PROCESSING),
        ("SUCCEEDED", NormalizedStatus.SUCCEEDED),
        ("FAILED", NormalizedStatus.FAILED),
        ("CANCELED", NormalizedStatus.CANCELLED),
    ],
)
def test_status_normalization(tmp_path, provider_status, normalized):
    provider, _, _ = make_provider(tmp_path, tasks=FakeTasks(provider_status))
    assert provider.get_status("task").status == normalized


def test_ambiguous_submission_and_cancellation(tmp_path):
    provider, _, tasks = make_provider(tmp_path, create=FakeCreate(ConnectionResetError("lost")))
    with pytest.raises(AmbiguousProviderSubmissionError, match="manual reconciliation"):
        provider.submit_generation(GenerationRequest("Prompt", "16:9", 5))
    assert provider.cancel("task").status == NormalizedStatus.CANCELLED
    assert tasks.deleted == "task"


class StreamResponse:
    def __init__(self, chunks, content_length=None):
        self.chunks = chunks
        self.headers = {} if content_length is None else {"content-length": str(content_length)}

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size):
        yield from self.chunks


class FakeHTTP:
    def __init__(self, response):
        self.response = response

    @contextmanager
    def stream(self, method, url):
        yield self.response


def test_successful_result_streams_to_disk(tmp_path):
    http = FakeHTTP(StreamResponse([b"video", b"-bytes"], 11))
    provider, _, _ = make_provider(
        tmp_path, tasks=FakeTasks("SUCCEEDED"), http_client=http, max_bytes=20
    )
    result = provider.get_result("task")
    assert result.status == NormalizedStatus.SUCCEEDED
    assert Path(result.outputs[0].uri).read_bytes() == b"video-bytes"
    assert not list(tmp_path.glob("*.partial"))


def test_download_limit_and_partial_cleanup(tmp_path):
    http = FakeHTTP(StreamResponse([b"12345", b"67890"]))
    provider, _, _ = make_provider(
        tmp_path, tasks=FakeTasks("SUCCEEDED"), http_client=http, max_bytes=6
    )
    with pytest.raises(DownloadError, match="size limit"):
        provider.get_result("task")
    assert not list(tmp_path.rglob("*.partial"))
    assert not list(tmp_path.glob("*.mp4"))


def test_cost_estimate_and_secret_redaction():
    settings.runway_video_duration_seconds = 5
    settings.runway_model_credits_per_second = 12
    settings.runway_credit_usd_rate = 0.01
    settings.runwayml_api_secret = SecretStr("test-secret-value")
    cost = estimate_cost()
    assert cost["estimated_credits"] == 60
    assert cost["estimated_usd"] == 0.60
    dirty = {
        "message": "Bearer abc test-secret-value",
        "authorization": "Bearer abc",
        "url": "https://example.test/video?token=sensitive&ok=yes",
    }
    clean = sanitize(dirty)
    assert "test-secret-value" not in str(clean)
    assert "Bearer abc" not in str(clean)
    assert "sensitive" not in str(clean)
    assert REDACTED in str(clean)
