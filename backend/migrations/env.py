from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, inspect, pool

try:
    from app.config import settings
    from app.db import (
        models as _models,  # noqa: F401  # registers all ORM models with Base.metadata
    )
    from app.db.base import Base
except ImportError:
    from backend.app.config import settings  # type: ignore[no-redef]
    from backend.app.db import models as _models  # type: ignore[no-redef] # noqa: F401
    from backend.app.db.base import Base  # type: ignore[no-redef]


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url_sync
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        command_options = getattr(config, "cmd_opts", None)
        destination = getattr(command_options, "destination_rev", None) or getattr(
            command_options, "revision", None
        )
        existing = set(inspect(connection).get_table_names()) - {"alembic_version"}
        # Inspector queries open an implicit SQLAlchemy 2 transaction. End it
        # before handing the connection to Alembic, otherwise an incremental
        # upgrade appears successful but is rolled back when the connection closes.
        connection.rollback()
        if destination in {"head", "heads"} and not existing:
            # The historical baseline imports today's ORM metadata. Replaying
            # later migrations after it therefore creates duplicate tables.
            # A truly empty database is instead bootstrapped atomically from
            # current metadata and stamped at the current head. Existing
            # installations still traverse every incremental migration.
            script = ScriptDirectory.from_config(config)
            head = script.get_current_head()
            if head is None:
                raise RuntimeError("Alembic has no single current head")
            with connection.begin():
                target_metadata.create_all(bind=connection)
                MigrationContext.configure(connection).stamp(script, head)
            return
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
