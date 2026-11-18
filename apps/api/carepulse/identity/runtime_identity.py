"""Gate-committed proof registration and transaction-local database authority."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from carepulse.compat.admission import (
    CompatRuntimeConfig,
    check_pool_checkout,
    is_compat_enabled,
)
from carepulse.identity.contracts import ContractError
from carepulse.identity.signing import CanonicalEnvelope


@dataclass(frozen=True, slots=True)
class RegisteredRequest:
    gate: Engine
    request_id: int
    identity: CanonicalEnvelope


current_request: ContextVar[RegisteredRequest | None] = ContextVar(
    "carepulse_registered_request", default=None
)


@contextmanager
def bound_transaction(
    engine: Engine, identity: CanonicalEnvelope | None = None, *, stage: str = "PRIMARY"
) -> Iterator[Connection]:
    """Activate only the registered proof, on this physical transaction.

    The gate's binding commits independently of business success. An aborted
    command therefore cannot re-use its primary stage or its transport nonce.
    """
    request = current_request.get()
    if request is None or (identity is not None and identity != request.identity):
        raise ContractError(403)
    with engine.begin() as connection:
        try:
            if is_compat_enabled():
                compat_cfg = CompatRuntimeConfig.from_env("api")
                if compat_cfg is not None:
                    check_pool_checkout(
                        connection, config=compat_cfg, expected_login="carepulse_api"
                    )
            pid, xid = connection.execute(
                text("SELECT pg_backend_pid(), pg_current_xact_id()::text")
            ).one()
            with request.gate.begin() as gate:
                if is_compat_enabled():
                    compat_cfg = CompatRuntimeConfig.from_env("api")
                    if compat_cfg is not None:
                        check_pool_checkout(
                            gate, config=compat_cfg, expected_login="carepulse_gate"
                        )
                gate.execute(
                    text(
                        "SELECT public.f01_bind(:request, :stage, :pid, CAST(:xid AS xid8))"
                    ),
                    {
                        "request": request.request_id,
                        "stage": stage,
                        "pid": pid,
                        "xid": xid,
                    },
                )
            connection.execute(text("SELECT public.f01_activate()"))
        except DBAPIError as denied:
            if getattr(denied.orig, "sqlstate", None) == "28000":
                raise ContractError(401) from denied
            if getattr(denied.orig, "sqlstate", None) == "42501":
                raise ContractError(403) from denied
            raise
        yield connection
