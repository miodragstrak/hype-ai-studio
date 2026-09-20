# Hype AI Studio

This spike proves a local asynchronous path for a music-video project: planned shots are submitted to a Redis-backed worker, a video provider creates MP4 variants, and the same shared backend package owns API and worker code. The deterministic mock provider remains the default; Runway Dev is available as an opt-in provider.

## Architecture

`frontend/` is a Vite producer workspace. `backend/app` is the shared FastAPI, domain, provider, storage, queue, and worker package. PostgreSQL is the lifecycle source of truth; Redis carries work notifications; local filesystem storage holds artifacts. The initial eight-table schema is extended by one `project_plans` table for immutable, versioned planning snapshots.

## Ubuntu setup

Prerequisites: Python 3.11+, Node 20+, Docker Compose, and FFmpeg with `libx264`, AAC,
and `ffprobe`. On Ubuntu, install both media commands with `sudo apt-get install ffmpeg`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
docker compose up -d postgres redis
psql "$DATABASE_URL" -f supabase/migrations/001_initial_schema.sql
psql "$DATABASE_URL" -f supabase/migrations/002_project_plans.sql
uvicorn backend.app.api:app --reload
python -m backend.app.worker
cd frontend && npm install && npm run dev
pytest
ruff check backend
mypy backend
```

The integration suite uses isolated PostgreSQL and Redis ports so it does not modify local
development data:

```bash
docker compose -p hype-studio-tests -f docker-compose.test.yml up -d --wait
pytest -m integration
```

Override `TEST_DATABASE_ADMIN_URL` or `TEST_REDIS_URL` when those test ports are unavailable.
Each run creates and drops uniquely named PostgreSQL databases, uses a unique Redis queue, and
removes generated files from the ignored `test-artifacts/` directory.

The API is available at `http://localhost:8000`; the frontend is normally `http://localhost:5173`. Upload test audio through `POST /projects/{project_id}/assets`. Use Docker Compose for full local dependencies with `docker compose up -d`.

## Producer review UI

The 001C React interface provides project setup, asset upload, shot planning and ordering,
asynchronous generation monitoring, variant selection/rejection, render readiness, and final MP4
preview/download. Its routes are `/projects`, `/projects/new`, and the Setup, Assets, Shots, Review,
and Render stages under `/projects/:projectId`.

Set `VITE_API_BASE_URL` when the API is not running on `http://localhost:8000`. For local work:

```bash
uvicorn backend.app.api:app --reload
python -m backend.app.worker
cd frontend && npm run dev
```

Frontend checks are `npm run lint`, `npm test`, and `npm run build`.

## Music video planning

The Plan stage at `/projects/:projectId/plan` submits a provider-neutral planning request to Redis.
The separate worker uses the deterministic mock planner by default, validates its structured result,
and stores an immutable draft. Producer edits create child versions; they never mutate an existing
snapshot. Approval validates the snapshot again and atomically materializes traceable shots without
starting video generation.

Set `PLANNING_PROVIDER=mock` for all local and test use. `MOCK_PLANNING_DELAY_SECONDS` makes async
behavior observable, while `MOCK_PLANNING_FAILURE_MODE` supports `transient:N`, `permanent`, and
`malformed` test cases. Planning and video providers are explicitly pinned to mocks in subprocess
integration tests, so the suite cannot make a paid provider request.

### Opt-in OpenAI planner

Set `PLANNING_PROVIDER=openai` and configure `OPENAI_API_KEY` only in the server-side environment to
use the official OpenAI SDK with the Responses API, Structured Outputs, and `gpt-5.6-sol`. Mock
planning remains the default. The adapter disables SDK retries, uses one request per worker attempt,
does not enable tools or web search, and never sends asset binaries.

OpenAI planning reserves estimated input plus maximum-output cost before submission under a database
lock. The development-wide soft alert defaults to `$2`; the hard cap defaults to `$5`. Authoritative
token usage, response ID, request timestamps, and clearly labeled estimated cost are stored in event
metadata. The application does not claim authoritative dollar cost when OpenAI only returns token
usage. A persisted parsed response is reused after worker redelivery; an ambiguous submission without
a response ID requires manual reconciliation.

The adapter currently uses the documented promotional `gpt-5.6-sol` rates of `$4` per million input
tokens and `$20` per million output tokens. These settings are explicit environment values so pricing
changes can be reviewed without changing orchestration code.

## Runway Dev

The worker integrates with Runway through the official Python SDK and the existing provider-neutral
contract. It is not live-verified yet. The mock remains the default; Runway is an explicit opt-in.
Create a local `.env` that is never committed and keep the organization key server-side:

```bash
export VIDEO_PROVIDER=runway
export RUNWAYML_API_SECRET=key_...
export RUNWAY_VIDEO_MODEL=gen4.5
export RUNWAY_VIDEO_DURATION_SECONDS=5
export RUNWAY_VIDEO_RATIO=1280:720
python -m backend.app.worker
```

The fixed spike request is Gen-4.5 text-to-video, five seconds, at `1280:720`. At the configured
12 credits per second and `$0.01` per credit, its estimate is 60 credits or `$0.60`. This estimate
is persisted with the attempt; no actual cost is invented when Runway does not return authoritative
task cost. These are temporary development-wide controls, not customer billing: crossing the `$10`
soft limit records a warning, while a projected total above the `$30` hard limit is rejected before
submission. Autobilling is not assumed or enabled.

### Image-to-video references

A shot generation may optionally select one project `REFERENCE_IMAGE` with JPEG, PNG, or WebP
content. The API validates the stored checksum, real image content, MIME type, 0.5–2 aspect ratio,
confirmed usage rights, and likeness consent whenever the metadata declares a real person. The
original remains in private application storage; the worker uses the official ephemeral Runway
upload and keeps the resulting `runway://` URI in memory only. Gen-4.5 then uses that upload as the
first frame for one five-second `1280:720` image-to-video task.

Reference asset ID and checksum are preserved in job, attempt, event, and variant evidence. An
approved derived crop can carry `derived_from_asset_id` in its rights metadata to retain provenance
to the original asset. Portrait or non-16:9 references produce a center-crop warning; the source is
never silently modified. Text-to-video remains available when the reference selector is empty.

The worker submits once, persists the task ID, polls without the SDK's blocking wait helper, and
copies successful output into local storage before the temporary URL expires. A submission exception
is treated as ambiguous and non-retryable because Runway may already have accepted a paid task;
manual reconciliation is required. The secret is redacted from persisted and returned errors.

After setting the secret and confirming account credits, the future controlled smoke test is:

```bash
VIDEO_PROVIDER=runway .venv/bin/python -m backend.app.worker
```

Start the API separately, create one five-second shot, and submit exactly one generation. Do not
commit `.env`. A successful real Runway integration must not be claimed until that paid path passes.

## Limitations

There is no authentication, production UI, Tour Guide workflow, cloud storage, hosted Supabase,
deployment, cost controls, or production monitoring. Runway usage requires an external account,
credits, and a server-side API key.
