# Hype AI Studio: HYPE-STUDIO-001A

This spike proves a local asynchronous path for a music-video project: planned shots are submitted to a Redis-backed worker, a deterministic mock provider creates MP4 variants, and the same shared backend package owns API and worker code. No real AI provider is integrated.

## Architecture

`frontend/` is a minimal Vite placeholder. `backend/app` is the shared FastAPI, domain, provider, storage, queue, and worker package. PostgreSQL is the lifecycle source of truth; Redis carries work notifications; local filesystem storage holds artifacts. The migration contains exactly the eight approved tables.

## Ubuntu setup

Prerequisites: Python 3.11+, Node 20+, Docker Compose, and FFmpeg with `libx264`, AAC,
and `ffprobe`. On Ubuntu, install both media commands with `sudo apt-get install ffmpeg`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
docker compose up -d postgres redis
psql "$DATABASE_URL" -f supabase/migrations/001_initial_schema.sql
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

## Limitations

There is no authentication, production UI, Tour Guide workflow, real provider, cloud storage, hosted Supabase, deployment, cost controls, or production monitoring. Only the deterministic mock provider and local filesystem adapter exist.
