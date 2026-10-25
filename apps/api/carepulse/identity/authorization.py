"""Live tenant and Better Auth session authority within one protected transaction."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from carepulse.db import lock_active_organization, transaction
from carepulse.identity.contracts import ContractError

if TYPE_CHECKING:
    from carepulse.identity.signing import CanonicalEnvelope


class AuthorizationDenied(ContractError):
    """No protected operation may run with absent or withdrawn live authority."""


@dataclass(frozen=True, slots=True)
class AuthorizedStaff:
    organization_id: UUID
    staff_id: UUID
    role: str
    bulk_reschedule: bool
    staff_revision: int
    security_revision: int
    lifecycle_revision: int


def _signed_actor(identity: "CanonicalEnvelope") -> tuple[UUID, UUID, int]:
    try:
        tenant = UUID(identity.organization_id)
        staff = UUID(identity.staff_id)
        revision = int(identity.security_revision)
    except (AttributeError, TypeError, ValueError) as invalid:
        raise AuthorizationDenied(401) from invalid
    if (
        revision < 0
        or not identity.session_id
        or not identity.role
        or not identity.purpose
        or identity.purpose.upper()
        in {
            "BOOTSTRAP",
            "RECOVERY",
            "FIRST_OWNER",
            "INVITE_ACCEPT",
            "PASSWORD_RESET",
            "OWNER_ACTIVATION",
        }
    ):
        raise AuthorizationDenied(401)
    return tenant, staff, revision


def bootstrap_owner_organization(identity: "CanonicalEnvelope") -> UUID:
    """Purpose-bound pre-ACTIVE identity grants only the bootstrap command."""
    if (
        identity.purpose != "OWNER_ACTIVATION"
        or identity.role != "BOOTSTRAP"
        or identity.session_id
        or identity.staff_id
        or identity.security_revision != "0"
    ):
        raise AuthorizationDenied(403)
    try:
        return UUID(identity.organization_id)
    except (TypeError, ValueError) as invalid:
        raise AuthorizationDenied(401) from invalid


def _current_staff(connection: Connection, tenant: UUID, staff: UUID):
    return (
        connection.execute(
            text("""
            SELECT s.auth_user_id, s.role, s.bulk_reschedule, s.revision,
                   s.security_revision, s.enabled, s.verified,
                   s.reset_block_operation_id, o.state, o.lifecycle_revision,
                   u.email, u."emailVerified", s.verified_email
              FROM public.staff_accounts AS s
              JOIN public.organizations AS o ON o.id = s.organization_id
              JOIN public."user" AS u ON u.id = s.auth_user_id
             WHERE s.organization_id = :tenant AND s.id = :staff
             FOR UPDATE OF s
        """),
            {"tenant": tenant, "staff": staff},
        )
        .mappings()
        .one_or_none()
    )


def _session_is_live(
    connection: Connection,
    identity: "CanonicalEnvelope",
    tenant: UUID,
    auth_user_id: str,
    idle_minutes: int,
) -> bool:
    session = (
        connection.execute(
            text("""
            SELECT "expiresAt", "absoluteDeadline", "lastActivity", "securityRevision"
              FROM public.lock_native_session_authority(:tenant, :user, :session)
        """),
            {"tenant": tenant, "session": identity.session_id, "user": auth_user_id},
        )
        .mappings()
        .one_or_none()
    )
    if session is None or session["securityRevision"] != int(
        identity.security_revision
    ):
        return False
    now = connection.scalar(text("SELECT clock_timestamp()"))
    return (
        now < session["expiresAt"]
        and now < session["absoluteDeadline"]
        and now < session["lastActivity"] + timedelta(minutes=idle_minutes)
    )


@contextmanager
def protected_transaction(
    engine: Engine, identity: "CanonicalEnvelope", idle_minutes: int
) -> Iterator[tuple[Connection, AuthorizedStaff]]:
    """Only a verified signed clinic request may cross this authority barrier."""
    tenant, staff_id, captured_revision = _signed_actor(identity)
    with transaction(engine, tenant) as connection:
        if not lock_active_organization(connection, tenant):
            raise AuthorizationDenied(403)
        staff = _current_staff(connection, tenant, staff_id)
        if staff is None:
            raise AuthorizationDenied(401)
        if (
            staff["state"] != "ACTIVE"
            or not staff["enabled"]
            or not staff["verified"]
            or staff["reset_block_operation_id"] is not None
            or staff["role"] != identity.role
            or not staff["emailVerified"]
            or staff["email"] != staff["verified_email"]
        ):
            raise AuthorizationDenied(403)
        if staff["security_revision"] != captured_revision or not _session_is_live(
            connection,
            identity,
            tenant,
            staff["auth_user_id"],
            idle_minutes,
        ):
            raise AuthorizationDenied(401)
        # Audited Doctor-only reads use this identity-bound transaction scope.
        # Never set it before the current staff/session checks succeed.
        connection.execute(
            text("SELECT set_config('app.staff_id', :staff, true)"),
            {"staff": str(staff_id)},
        )
        yield (
            connection,
            AuthorizedStaff(
                tenant,
                staff_id,
                staff["role"],
                staff["bulk_reschedule"],
                staff["revision"],
                staff["security_revision"],
                staff["lifecycle_revision"],
            ),
        )
