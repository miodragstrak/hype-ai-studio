import json

from redis import Redis

from backend.app.config import settings


def client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def enqueue(job_id: str, kind: str = "generation") -> None:
    client().lpush(settings.queue_name, json.dumps({"job_id": job_id, "kind": kind}))


def dequeue(timeout: int = 1) -> dict[str, str] | None:
    item = client().brpop(settings.queue_name, timeout=timeout)
    if item is None:
        return None
    return json.loads(item[1])
