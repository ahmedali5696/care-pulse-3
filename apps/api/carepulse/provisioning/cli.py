"""One-shot restricted metadata-only operator command line."""

import argparse
import getpass
import json
import sys
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from carepulse.config import ConfigError, load_config
from carepulse.db import create_db_engine
from carepulse.identity.receipts import ReceiptConflict
from carepulse.provisioning.invitations import (
    create_clinic,
    reissue_invitation,
    revoke_invitation,
)
from carepulse.provisioning.operators import OperatorDenied, authenticate_operator
from carepulse.provisioning.service import change_clinic_state, list_clinics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="carepulse-admin")
    clinics = parser.add_subparsers(dest="group", required=True)
    clinic_parser = clinics.add_parser("clinics")
    operations = clinic_parser.add_subparsers(dest="action", required=True)
    for action in (
        "create",
        "list",
        "invite",
        "revoke-invite",
        "suspend",
        "reactivate",
    ):
        command = operations.add_parser(action)
        command.add_argument("--operator-id", required=True, type=UUID)
        command.add_argument("--reason", required=True)
        if action == "create":
            command.add_argument("--branch", required=True)
            command.add_argument("--owner-email", required=True)
            command.add_argument("--idempotency-key", required=True)
        if action in ("invite", "revoke-invite", "suspend", "reactivate"):
            command.add_argument("--clinic-id", required=True, type=UUID)
            command.add_argument(
                "--expected-lifecycle-revision", required=True, type=int
            )
        if action in ("invite", "revoke-invite"):
            command.add_argument(
                "--invitation-id", required=action == "revoke-invite", type=UUID
            )
            command.add_argument(
                "--expected-invitation-revision", required=True, type=int
            )
        if action == "invite":
            command.add_argument("--owner-email")
    return parser


def _execute(args, engine, operator, config):
    if args.action == "list":
        return {"items": list_clinics(engine, operator, args.reason)}
    if args.action == "create":
        return create_clinic(
            engine,
            operator,
            config,
            args.branch,
            args.owner_email,
            args.idempotency_key,
            args.reason,
        )
    if args.expected_lifecycle_revision < 0:
        raise ValueError("Negative expected lifecycle revision")
    if args.action in ("suspend", "reactivate"):
        return change_clinic_state(
            engine,
            operator,
            args.clinic_id,
            args.expected_lifecycle_revision,
            "SUSPENDED" if args.action == "suspend" else "ACTIVE",
            args.reason,
        )
    if args.expected_invitation_revision < 0:
        raise ValueError("Negative expected invitation revision")
    if args.action == "invite":
        return reissue_invitation(
            engine,
            operator,
            config,
            args.clinic_id,
            args.invitation_id,
            args.expected_lifecycle_revision,
            args.expected_invitation_revision,
            args.reason,
            args.owner_email,
        )
    return revoke_invitation(
        engine,
        operator,
        args.clinic_id,
        args.invitation_id,
        args.expected_lifecycle_revision,
        args.expected_invitation_revision,
        args.reason,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        not args.reason.strip()
        or len(args.reason) > 500
        or any(ord(char) < 32 or ord(char) == 127 for char in args.reason)
    ):
        print("Operator action requires a nonempty bounded reason", file=sys.stderr)
        return 2
    engine = None
    try:
        config = load_config("operator")
        secret = config.operator_secret or getpass.getpass(
            "Individual operator credential: "
        )
        engine = create_db_engine(config)
        operator = authenticate_operator(
            engine, args.operator_id, secret, config.policy
        )
        output = _execute(args, engine, operator, config)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        ConfigError,
        OperatorDenied,
        ReceiptConflict,
        ValueError,
        SQLAlchemyError,
        EOFError,
        KeyboardInterrupt,
    ):
        print("Operator command unavailable, denied, or in conflict", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
