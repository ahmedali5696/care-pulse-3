"""Shared synchronous runtime pool and transaction-local organization context."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import URL, Connection, Engine, create_engine, text

from carepulse.compat.admission import (
    CompatRuntimeConfig,
    check_pool_checkout,
    is_compat_enabled,
)
from carepulse.config import ConfigError, ServiceConfig


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


def create_gate_engine(config: ServiceConfig) -> Engine:
    if config.db_user != "carepulse_api" or not config.gate_db_password:
        raise ConfigError("Gate database requires API configuration")
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username="carepulse_gate",
            password=config.gate_db_password,
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
        if is_compat_enabled():
            compat_cfg = CompatRuntimeConfig.from_env("api")
            if compat_cfg is not None:
                check_pool_checkout(connection, config=compat_cfg)
        connection.execute(
            text("SELECT set_config('app.organization_id', :organization_id, true)"),
            {"organization_id": str(organization_id) if organization_id else ""},
        )
        yield connection


def lock_active_organization(connection: Connection, organization_id: UUID) -> bool:
    """Lock a live clinic row without giving the runtime role mutation grants."""
    if not isinstance(organization_id, UUID):
        raise TypeError("Organization lock requires a validated UUID")
    return bool(
        connection.scalar(
            text("SELECT public.lock_active_organization(:tenant)"),
            {"tenant": organization_id},
        )
    )
