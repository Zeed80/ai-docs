"""Log ORM tables/columns missing from the live schema after migrations.

Non-fatal on purpose: failing the API start would turn a partial outage into
a restart loop. Exit code 1 lets callers react if they choose to.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine

from app.config import settings
from app.db import models as _models  # noqa: F401  # registers ORM models
from app.db.base import Base
from app.db.schema_drift import missing_schema_objects


def main() -> int:
    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as connection:
            missing = missing_schema_objects(connection, Base.metadata)
    finally:
        engine.dispose()
    if missing:
        print(
            "=== SCHEMA DRIFT: alembic is at head but the schema lacks: "
            + ", ".join(missing)
            + " ===",
            file=sys.stderr,
        )
        return 1
    print("=== Schema check: no missing tables/columns ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
