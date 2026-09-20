from typing import Protocol

from backend.app.domain.models import GenerationStatus
from backend.app.domain.planning import PlanningRequest, PlanningResult


class PlanningProvider(Protocol):
    provider: str
    model: str

    def submit_plan(self, request: PlanningRequest) -> str: ...
    def get_status(self, provider_job_id: str) -> GenerationStatus: ...
    def get_result(self, provider_job_id: str) -> PlanningResult: ...
    def cancel(self, provider_job_id: str) -> GenerationStatus: ...
