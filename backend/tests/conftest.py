from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from redis import Redis

from backend.app.api import app
from backend.app.config import settings

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((ROOT / "supabase/migrations").glob("*.sql"))
ADMIN_URL = os.getenv(
    "TEST_DATABASE_ADMIN_URL",
    "postgresql://hype_test:hype_test@127.0.0.1:55432/postgres",
)
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:56379/0")


@pytest.fixture
def artifact_dir() -> Iterator[Path]:
    path = ROOT / "test-artifacts" / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    database_name = f"hype_test_{uuid4().hex}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database_name}"')
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{database_name}"
    with psycopg.connect(url) as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text())
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=%s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        conn.execute(f'DROP DATABASE "{database_name}"')


@pytest.fixture(scope="session")
def integration_environment(database_url: str):
    redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    assert redis.ping()
    storage_root = ROOT / "test-artifacts" / uuid4().hex
    storage_root.mkdir(parents=True)
    settings.database_url = database_url
    settings.redis_url = TEST_REDIS_URL
    settings.storage_root = storage_root
    settings.queue_name = f"hype:test:{uuid4().hex}"
    environment = {
        "database_url": database_url,
        "redis": redis,
        "storage_root": storage_root,
        "queue_name": settings.queue_name,
    }
    yield environment
    redis.delete(settings.queue_name)
    shutil.rmtree(storage_root)


@pytest.fixture(autouse=True)
def clean_state(integration_environment):
    with psycopg.connect(integration_environment["database_url"]) as conn:
        conn.execute(
            "TRUNCATE events,renders,shot_variants,generation_attempts,shots,project_plans,jobs,assets,projects "
            "RESTART IDENTITY CASCADE"
        )
    integration_environment["redis"].delete(integration_environment["queue_name"])
    settings.mock_provider_delay_seconds = 0.05
    settings.mock_provider_failure_mode = "none"
    settings.planning_provider = "mock"
    settings.mock_planning_delay_seconds = 0.05
    settings.mock_planning_failure_mode = "none"
    settings.video_provider = "mock"
    settings.runwayml_api_secret = None
    settings.runway_soft_limit_usd = 10
    settings.runway_hard_limit_usd = 30
    settings.runway_poll_interval_seconds = 0.01
    settings.runway_task_timeout_seconds = 2
    settings.max_retries = 2


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(database_url: str):
    @contextmanager
    def connect():
        with psycopg.connect(database_url) as conn:
            yield conn

    return connect


@pytest.fixture
def worker_process(
    integration_environment,
) -> Iterator[Callable[..., subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []

    def start(
        *,
        failure_mode: str = "none",
        delay: float = 0.05,
        planning_failure_mode: str = "none",
        planning_delay: float = 0.05,
    ) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env.update(
            DATABASE_URL=integration_environment["database_url"],
            REDIS_URL=TEST_REDIS_URL,
            STORAGE_ROOT=str(integration_environment["storage_root"]),
            QUEUE_NAME=integration_environment["queue_name"],
            VIDEO_PROVIDER="mock",
            PLANNING_PROVIDER="mock",
            RUNWAYML_API_SECRET="",
            MOCK_PROVIDER_DELAY_SECONDS=str(delay),
            MOCK_PROVIDER_FAILURE_MODE=failure_mode,
            MOCK_PLANNING_DELAY_SECONDS=str(planning_delay),
            MOCK_PLANNING_FAILURE_MODE=planning_failure_mode,
            MAX_RETRIES=str(settings.max_retries),
            FFMPEG_EXECUTABLE=settings.ffmpeg_executable,
            FFPROBE_EXECUTABLE=settings.ffprobe_executable,
            PYTHONUNBUFFERED="1",
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "backend.app.worker"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


@pytest.fixture
def wait_for() -> Callable[..., dict]:
    def wait(fetch: Callable[[], dict], predicate: Callable[[dict], bool], timeout: float = 10):
        deadline = time.monotonic() + timeout
        last = fetch()
        while time.monotonic() < deadline:
            if predicate(last):
                return last
            time.sleep(0.05)
            last = fetch()
        pytest.fail(f"timed out after {timeout}s; last observed value: {last}")

    return wait
