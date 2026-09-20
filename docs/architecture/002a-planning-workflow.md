# ADR: Music Video Planning Workflow

## Status

Accepted for HYPE-STUDIO-002A.

## Context

Music video producers need a structured creative treatment and shot plan before video generation.
Planning can be slow, must preserve revision history, and must not couple the domain model to a
specific AI provider.

## Decision

Planning uses the existing FastAPI, Redis, worker, and PostgreSQL modular monolith. The API stores a
`PLAN_GENERATION` job with a validated provider-neutral request and returns `202`. A separate worker
submits that request to the configured planning adapter, validates the normalized result, and stores
one immutable `project_plans` snapshot linked to the source job.

Producer edits create a new draft with a parent-plan link and a monotonically increasing project
version. Approval runs in one database transaction, validates duration, shot count, ordering, and
asset ownership again, then creates shots carrying `source_plan_id` and `source_plan_item_key`.
Repeated approval is idempotent and does not create duplicate shots or generation jobs.

Only one plan may be approved per project. Replacing it is allowed while its materialized shots are
unchanged and have no generation attempts. Replacement is blocked after producer divergence or
generation history. Manual shots have no plan provenance and are preserved.

## Provider Boundary

`PlanningProvider` exposes submit, status, result, and cancel operations. Provider-specific data
stays inside adapters. The 002A implementation ships only a deterministic mock adapter with bounded
delay and controlled transient, permanent, and malformed-result modes. Tests force both planning and
video configuration to mocks and clear the Runway secret in worker subprocesses.

## Consequences

Plans are auditable and reproducible, approval cannot silently launch billable generation, and the
existing job/event infrastructure covers lifecycle visibility. The single new table is accompanied
only by provenance columns on `shots` and request data on `jobs`; no new service boundary is added.
