"""Container-free transaction lifecycle checks; no PostgreSQL/RLS isolation claim."""

import sqlite3
from asyncio import CancelledError

import pytest
from carepulse.db import transaction
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def engine(tmp_path):
    database = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")

    @event.listens_for(database, "connect")
    def sqlite_context_placeholder(connection, _record):
        # Lifecycle unit tests have no tenant; PostgreSQL RLS is proven in CI.
        def set_config(_key, organization, _local):
            if organization:
                raise sqlite3.OperationalError("SQLite has no tenant context")
            return ""

        connection.create_function("set_config", 3, set_config)

    with database.begin() as connection:
        connection.execute(text("CREATE TABLE lifecycle (value integer)"))
    yield database
    database.dispose()


def test_success_commits_and_releases_connection(engine):
    with transaction(engine) as connection:
        connection.execute(text("INSERT INTO lifecycle VALUES (1)"))
    assert connection.closed
    assert engine.pool.checkedout() == 0
    with engine.connect() as next_connection:
        assert next_connection.scalar(text("SELECT value FROM lifecycle")) == 1


@pytest.mark.parametrize("failure", [RuntimeError, CancelledError])
def test_exception_or_cancellation_rolls_back_and_releases(engine, failure):
    with pytest.raises(failure), transaction(engine) as connection:
        connection.execute(text("INSERT INTO lifecycle VALUES (1)"))
        raise failure("Synthetic lifecycle interruption")
    assert connection.closed
    assert engine.pool.checkedout() == 0
    with engine.connect() as next_connection:
        assert next_connection.scalar(text("SELECT count(*) FROM lifecycle")) == 0


def test_commit_failure_releases_without_persisting_partial_work(engine):
    def fail_commit(connection):
        raise RuntimeError("Synthetic commit failure")

    event.listen(engine, "commit", fail_commit)
    with (
        pytest.raises(RuntimeError, match="Synthetic commit failure"),
        transaction(engine) as connection,
    ):
        connection.execute(text("INSERT INTO lifecycle VALUES (1)"))
    event.remove(engine, "commit", fail_commit)
    assert connection.closed
    assert engine.pool.checkedout() == 0
    with engine.connect() as next_connection:
        assert next_connection.scalar(text("SELECT count(*) FROM lifecycle")) == 0


def test_context_setup_failure_releases_before_yielding(engine):
    from uuid import UUID

    # The SQLite lifecycle fixture explicitly refuses tenant context.
    with pytest.raises(DBAPIError), transaction(engine, UUID(int=1)):
        pytest.fail("A failed context setup yielded a connection")
    assert engine.pool.checkedout() == 0


def test_unvalidated_context_is_rejected_before_checkout(engine):
    with (
        pytest.raises(ValueError, match="validated UUID"),
        transaction(engine, "browser-supplied-context"),
    ):
        pytest.fail("Unvalidated organization context was accepted")
    assert engine.pool.checkedout() == 0
