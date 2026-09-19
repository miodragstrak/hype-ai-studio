from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from backend.app.config import settings

_BUDGET_LOCK_ID = 4_861_579_001


class BudgetExceededError(RuntimeError):
    retryable = False
    code = "RUNWAY_HARD_LIMIT_EXCEEDED"


def estimate_cost() -> dict[str, Any]:
    duration = settings.runway_video_duration_seconds
    credits = Decimal(str(settings.runway_model_credits_per_second)) * duration
    usd = credits * Decimal(str(settings.runway_credit_usd_rate))
    return {
        "provider": "runway",
        "model": settings.runway_video_model,
        "duration_seconds": duration,
        "credits_per_second": float(settings.runway_model_credits_per_second),
        "estimated_credits": float(credits),
        "credit_usd_rate": float(settings.runway_credit_usd_rate),
        "estimated_usd": float(usd),
        "actual_credits": None,
        "actual_usd": None,
        "cost_source": "configured_runway_pricing",
        "budget_status": "reserved",
    }


def reserve_attempt(cursor, job: tuple, shot: tuple) -> tuple[str | None, bool]:
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_BUDGET_LOCK_ID,))
    cursor.execute(
        "SELECT COALESCE(sum((cost_metadata->>'estimated_usd')::numeric),0) "
        "FROM generation_attempts WHERE provider='runway' "
        "AND cost_metadata->>'budget_status'='reserved'"
    )
    cumulative = Decimal(cursor.fetchone()[0])
    cost = estimate_cost()
    projected = cumulative + Decimal(str(cost["estimated_usd"]))
    attempt_number = job[3] + 1
    if projected > Decimal(str(settings.runway_hard_limit_usd)):
        cost["budget_status"] = "rejected"
        cursor.execute(
            "INSERT INTO generation_attempts "
            "(project_id,shot_id,job_id,provider,model,submitted_prompt,parameters,attempt_number,status,started_at,completed_at,cost_metadata,error_data) "
            "VALUES (%s,%s,%s,'runway',%s,%s,%s,%s,'REJECTED',now(),now(),%s,%s) RETURNING id",
            (
                job[1], job[2], job[0], settings.runway_video_model, shot[0], Jsonb({}),
                attempt_number, Jsonb(cost), Jsonb({"code": BudgetExceededError.code, "retryable": False}),
            ),
        )
        attempt_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) "
            "VALUES (%s,'RUNWAY_HARD_LIMIT_REJECTED','worker','generation_attempt',%s,%s)",
            (job[1], attempt_id, Jsonb({"projected_usd": float(projected), "hard_limit_usd": settings.runway_hard_limit_usd})),
        )
        return None, False
    cursor.execute(
        "INSERT INTO generation_attempts "
        "(project_id,shot_id,job_id,provider,model,submitted_prompt,parameters,attempt_number,status,started_at,cost_metadata) "
        "VALUES (%s,%s,%s,'runway',%s,%s,%s,%s,'RESERVED',now(),%s) RETURNING id",
        (job[1], job[2], job[0], settings.runway_video_model, shot[0], Jsonb({}), attempt_number, Jsonb(cost)),
    )
    attempt_id = cursor.fetchone()[0]
    warned = projected >= Decimal(str(settings.runway_soft_limit_usd))
    if warned:
        cursor.execute(
            "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) "
            "VALUES (%s,'RUNWAY_SOFT_LIMIT_WARNING','worker','generation_attempt',%s,%s)",
            (job[1], attempt_id, Jsonb({"projected_usd": float(projected), "soft_limit_usd": settings.runway_soft_limit_usd})),
        )
    return str(attempt_id), warned
