import os
from pathlib import Path

# Tests always use the committed example roster, never a developer's local one.
os.environ["ROSTER_PATH"] = str(Path(__file__).resolve().parents[1] / "config" / "roster.example.json")

import psycopg  # noqa: E402
import pytest  # noqa: E402

from chatbot.config import database_url  # noqa: E402
from chatbot.tracing.store import PRODUCTION_DATABASE  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def drop_test_schemas():
    """Each test's store lives in its own schema (named t_...). Drop them all when the run ends.

    The tests need the Postgres container: docker compose -f docker/compose.yml up -d postgres
    """
    dsn = database_url()
    if psycopg.conninfo.conninfo_to_dict(dsn).get("dbname") == PRODUCTION_DATABASE:
        raise RuntimeError("the test suite must not run against the production database")
    yield
    with psycopg.connect(dsn, autocommit=True) as c:
        names = [r[0] for r in c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 't\\_%'").fetchall()]
        for name in names:
            c.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(name)))
