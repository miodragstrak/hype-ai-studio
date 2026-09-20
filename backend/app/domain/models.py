from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    PROVIDER_PENDING = "PROVIDER_PENDING"
    PROCESSING = "PROCESSING"
    DOWNLOADING = "DOWNLOADING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class VariantReviewStatus(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class NormalizedStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ReferenceImageInput:
    asset_id: str
    checksum: str
    path: str
    mime_type: str


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    aspect_ratio: str
    target_duration: float
    reference_asset_ids: list[str] = field(default_factory=list)
    reference_image: ReferenceImageInput | None = None
    correlation_id: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputReference:
    uri: str
    mime_type: str = "video/mp4"


@dataclass(frozen=True)
class NormalizedError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationStatus:
    provider_job_id: str
    status: NormalizedStatus
    metadata: dict[str, Any] = field(default_factory=dict)
    error: NormalizedError | None = None


@dataclass(frozen=True)
class GenerationResult:
    provider_job_id: str
    status: NormalizedStatus
    outputs: list[OutputReference] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    timing_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    error: NormalizedError | None = None
