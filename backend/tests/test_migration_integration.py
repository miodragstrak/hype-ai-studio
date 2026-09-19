from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from backend.tests.conftest import ADMIN_URL, MIGRATION

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "projects",
    "assets",
    "shots",
    "jobs",
    "generation_attempts",
    "shot_variants",
    "renders",
    "events",
}


def test_migration_on_clean_database_has_exact_schema_and_constraints():
    database_name = f"hype_migration_{uuid4().hex}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{database_name}"
    try:
        with psycopg.connect(url) as conn:
            conn.execute(MIGRATION.read_text())
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                ).fetchall()
            }
            assert tables == EXPECTED_TABLES
            project_id = conn.execute(
                "INSERT INTO projects(project_type,title) VALUES ('MUSIC_VIDEO','constraints') RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO shots(project_id,ordinal,prompt,intended_duration) VALUES (%s,1,'a',1)",
                (project_id,),
            )
            conn.commit()
            with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO shots(project_id,ordinal,prompt,intended_duration) VALUES (%s,1,'b',1)",
                    (project_id,),
                )
            with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO assets(project_id,asset_type,storage_key,mime_type,size_bytes) VALUES (%s,'AUDIO','x','audio/wav',1)",
                    (uuid4(),),
                )
            constraints = {
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT constraint_name,constraint_type FROM information_schema.table_constraints "
                    "WHERE table_schema='public'"
                ).fetchall()
            }
            assert {
                ("shots_project_id_ordinal_key", "UNIQUE"),
                ("jobs_idempotency_key_key", "UNIQUE"),
                ("shots_selected_variant_fk", "FOREIGN KEY"),
                ("assets_project_id_fkey", "FOREIGN KEY"),
                ("generation_attempts_job_id_fkey", "FOREIGN KEY"),
            }.issubset(constraints)
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                ).fetchall()
            }
            assert "one_selected_variant_per_shot" in indexes
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
