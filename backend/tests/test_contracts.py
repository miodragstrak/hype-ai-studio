from pathlib import Path

import pytest

from backend.app.domain.models import GenerationRequest, NormalizedStatus
from backend.app.providers.mock import MockVideoProvider
from backend.app.storage.local import LocalStorage


def test_mock_provider_success(artifact_dir: Path):
    provider = MockVideoProvider(artifact_dir, delay_seconds=0)
    job_id = provider.submit_generation(GenerationRequest("test", "16:9", 0.2))
    assert provider.get_status(job_id).status is NormalizedStatus.SUCCEEDED
    result = provider.get_result(job_id)
    assert result.outputs and Path(result.outputs[0].uri).stat().st_size > 0


def test_mock_provider_controlled_failure(tmp_path: Path):
    provider = MockVideoProvider(tmp_path, delay_seconds=0, failure_mode="permanent")
    job_id = provider.submit_generation(GenerationRequest("test", "16:9", 0.2))
    result = provider.get_result(job_id)
    assert result.status is NormalizedStatus.FAILED and result.error is not None


def test_storage_rejects_traversal(tmp_path: Path):
    storage = LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.save("../../outside", b"bad")
