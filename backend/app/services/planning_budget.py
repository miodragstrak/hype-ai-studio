from __future__ import annotations

import math
from typing import Any

from psycopg.types.json import Jsonb

from backend.app.config import settings

_BUDGET_LOCK_ID = 2_002_002


class PlanningBudgetExceededError(RuntimeError):
    code = "OPENAI_PLANNING_BUDGET_EXCEEDED"
    retryable = False


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def estimate_cost(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    input_cost = input_tokens * settings.openai_planning_input_usd_per_million / 1_000_000
    output_cost = output_tokens * settings.openai_planning_output_usd_per_million / 1_000_000
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_input_cost_usd": round(input_cost, 8),
        "estimated_output_cost_usd": round(output_cost, 8),
        "estimated_total_cost_usd": round(input_cost + output_cost, 8),
        "actual_cost_usd": None,
    }


def reserve_budget(cursor, project_id, job_id, prompt: str) -> dict[str, Any]:
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_BUDGET_LOCK_ID,))
    cursor.execute(
        "SELECT payload FROM events WHERE event_type='OPENAI_PLANNING_BUDGET_RESERVED' "
        "AND entity_id=%s",
        (job_id,),
    )
    existing = cursor.fetchone()
    if existing:
        return existing[0]
    estimate = estimate_cost(estimate_tokens(prompt), settings.openai_planning_max_output_tokens)
    cursor.execute(
        "SELECT COALESCE(sum((payload->>'cost_delta_usd')::numeric),0) FROM events "
        "WHERE event_type IN ('OPENAI_PLANNING_BUDGET_RESERVED','OPENAI_PLANNING_BUDGET_RECONCILED')"
    )
    cumulative = float(cursor.fetchone()[0])
    projected = cumulative + estimate["estimated_total_cost_usd"]
    if projected > settings.openai_planning_hard_limit_usd:
        raise PlanningBudgetExceededError("OpenAI planning hard spending limit would be exceeded")
    payload = {
        **estimate,
        "cost_delta_usd": estimate["estimated_total_cost_usd"],
        "cumulative_reserved_usd": round(projected, 8),
    }
    cursor.execute(
        "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) "
        "VALUES (%s,'OPENAI_PLANNING_BUDGET_RESERVED','worker','job',%s,%s)",
        (project_id, job_id, Jsonb(payload)),
    )
    if cumulative < settings.openai_planning_soft_limit_usd <= projected:
        cursor.execute(
            "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) "
            "VALUES (%s,'OPENAI_PLANNING_SOFT_LIMIT_REACHED','worker','job',%s,%s)",
            (project_id, job_id, Jsonb({"cumulative_estimated_spend_usd": round(projected, 8)})),
        )
    return payload


def reconcile_budget(cursor, project_id, job_id, reserved: dict, usage: dict) -> dict[str, Any]:
    actual_estimate = estimate_cost(
        int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    )
    delta = actual_estimate["estimated_total_cost_usd"] - reserved["estimated_total_cost_usd"]
    payload = {**actual_estimate, "cost_delta_usd": round(delta, 8), "authoritative_usage": usage}
    cursor.execute(
        "INSERT INTO events(project_id,event_type,actor_type,entity_type,entity_id,payload) "
        "VALUES (%s,'OPENAI_PLANNING_BUDGET_RECONCILED','worker','job',%s,%s)",
        (project_id, job_id, Jsonb(payload)),
    )
    return payload
