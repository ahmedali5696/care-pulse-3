"""Shared synchronous runtime pool and transaction-local organization context."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import URL, Connection, Engine, create_engine, text

from carepulse.config import ServiceConfig


def create_db_engine(config: ServiceConfig) -> Engine:
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username=config.db_user,
            password=config.db_password,
            host=config.db_host,
            port=config.db_port,
            database=config.db_name,
        ),
        isolation_level="READ COMMITTED",
        hide_parameters=True,
    )


@contextmanager
def transaction(
    engine: Engine, organization_id: UUID | None = None
) -> Iterator[Connection]:
    """Only callers with validated server authority may supply an organization."""
    if organization_id is not None and not isinstance(organization_id, UUID):
        raise ValueError("Organization context must be a validated UUID")
    with engine.begin() as connection:
        if organization_id is not None:
            connection.execute(
                text(
                    "SELECT set_config('app.organization_id', :organization_id, true)"
                ),
                {"organization_id": str(organization_id)},
            )
        yield connection
