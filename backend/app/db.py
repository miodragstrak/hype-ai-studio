from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from backend.app.config import settings


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url) as conn:
        yield conn


def execute(sql: str, params: tuple = ()) -> None:
    with connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
