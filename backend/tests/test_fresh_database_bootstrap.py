"""An empty database bootstrapped by ``alembic upgrade head`` works like a migrated one.

The bootstrap creates today's metadata and stamps head, skipping migrations;
the extensions those migrations create were skipped too. In the E52 eval world
every ``similarity()`` search answered 500 — catalog cases failed 0/3 twice.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_upgrade_head_on_an_empty_database_installs_pg_trgm():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("needs TEST_DATABASE_URL")
    name = f"aiw_bootstrap_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        env = {**os.environ, "POSTGRES_DB": name}
        done = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert done.returncode == 0, done.stderr[-2000:]
        fresh = await asyncpg.connect(url.rsplit("/", 1)[0] + f"/{name}")
        try:
            assert (
                await fresh.fetchval("SELECT similarity('проволока'::varchar, 'проволка'::varchar)")
                > 0
            )
        finally:
            await fresh.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
