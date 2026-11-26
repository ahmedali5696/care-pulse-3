"""Migration startup evidence must not disclose raw driver diagnostics."""

from unittest.mock import patch

from psycopg.errors import CheckViolation
from sqlalchemy.exc import DBAPIError

from ops.migration_startup import main


def test_migration_startup_redacts_driver_error(capsys):
    error = DBAPIError(
        "SELECT confidential_statement",
        {"secret": "confidential_value"},
        CheckViolation("confidential_message"),
    )
    with patch("ops.migration_startup.command.upgrade", side_effect=error):
        assert main() == 1
    assert capsys.readouterr().out == (
        "MIGRATION_STARTUP FAIL category=database sqlstate=23514 location=unavailable\n"
    )


def test_migration_startup_reports_success_only_after_upgrade(capsys):
    with patch("ops.migration_startup.command.upgrade") as upgrade:
        assert main() == 0
    upgrade.assert_called_once()
    assert upgrade.call_args.args[1] == "head"
    assert capsys.readouterr().out == "MIGRATION_STARTUP PASS\n"
