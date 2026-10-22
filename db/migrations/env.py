"""Explicit migration credential only; application startup never imports this."""

from alembic import context
from carepulse.config import _host, _port, _required, _secret
from sqlalchemy import URL, Connection, create_engine
from sqlalchemy.pool import NullPool


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Foundation migrations require an explicit online connection")

connection = context.config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_migration",
            password=_secret("CP_MIGRATION_PASSWORD_FILE", minimum=16),
            host=_host("CP_DB_HOST"),
            port=_port("CP_DB_PORT"),
            database=_required("CP_DB_NAME"),
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()
