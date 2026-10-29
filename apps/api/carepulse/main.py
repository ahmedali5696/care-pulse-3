"""Private API entrypoint; register no clinical routes until their owning tasks."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from re import compile

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from carepulse.config import load_config
from carepulse.db import create_db_engine
from carepulse.health import router as health_router
from carepulse.identity.boundary import ClosedServiceBoundary
from carepulse.identity.contracts import (
    ContractError,
    contract_exception_handler,
    error_response,
)
from carepulse.identity.receipts import ReceiptConflict
from carepulse.identity.staff_routes import router as staff_router
from carepulse.patients.routes import router as patient_router
from carepulse.policies.pricing_routes import router as pricing_router
from carepulse.policies.schedule_routes import router as schedule_router
from carepulse.policies.slots_routes import router as slots_router
from carepulse.provisioning.routes import router as provisioning_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Refuse protected startup before initializing any operational capability."""
    app.state.service_config = load_config("api")
    app.state.db_engine = create_db_engine(app.state.service_config)
    try:
        yield
    finally:
        app.state.db_engine.dispose()


app = FastAPI(
    title="CarePulse Foundation",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(provisioning_router)
app.include_router(staff_router)
app.include_router(patient_router)
app.include_router(pricing_router)
app.include_router(schedule_router)
app.include_router(slots_router)
app.add_exception_handler(ContractError, contract_exception_handler)
app.add_exception_handler(
    RequestValidationError, lambda _request, _error: error_response(422)
)
app.add_exception_handler(SQLAlchemyError, lambda _request, _error: error_response(503))
app.add_exception_handler(
    ReceiptConflict,
    lambda _request, _error: error_response(409, code="IDEMPOTENCY_CONFLICT"),
)
app.add_middleware(
    ClosedServiceBoundary,
    routes={
        ("POST", "/v1/provisioning/owner-activate"): "OWNER_ACTIVATION",
        ("GET", "/v1/staff/receptionists"): "STAFF_MANAGEMENT",
        ("POST", "/v1/staff/receptionists"): "STAFF_MANAGEMENT",
        ("POST", "/v1/staff/receptionists/invite-accept"): "RECEPTIONIST_ACCEPT",
        ("POST", "/v1/staff/recovery/reserve"): "OWNER_RECOVERY",
        ("POST", "/v1/staff/recovery/finalize"): "OWNER_RECOVERY",
        ("GET", "/v1/patients"): "PATIENT_REGISTRY",
        ("POST", "/v1/patients"): "PATIENT_REGISTRY",
        ("GET", "/v1/settings/pricing"): "SETTINGS_PRICING",
        ("PATCH", "/v1/settings/pricing"): "SETTINGS_PRICING",
        ("GET", "/v1/settings/schedule"): "SETTINGS_SCHEDULE",
        ("PATCH", "/v1/settings/schedule"): "SETTINGS_SCHEDULE",
        ("POST", "/v1/settings/schedule/preview"): "SETTINGS_SCHEDULE",
        ("POST", "/v1/settings/schedule/activate"): "SETTINGS_SCHEDULE",
        ("GET", "/v1/slots"): "SLOTS",
    },
    route_patterns=(
        (
            "GET",
            compile(
                r"/v1/patients/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
            ),
            "PATIENT_REGISTRY",
        ),
        (
            "PATCH",
            compile(
                r"/v1/patients/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
            ),
            "PATIENT_REGISTRY",
        ),
        (
            "POST",
            compile(
                r"/v1/patients/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/(?:archive|unarchive)"
            ),
            "PATIENT_REGISTRY",
        ),
        (
            "GET",
            compile(
                r"/v1/staff/receptionists/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
            ),
            "STAFF_MANAGEMENT",
        ),
        (
            "POST",
            compile(
                r"/v1/staff/receptionists/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/(?:enable|disable|invite|revoke-invite)"
            ),
            "STAFF_MANAGEMENT",
        ),
        (
            "PATCH",
            compile(
                r"/v1/staff/receptionists/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/permissions"
            ),
            "STAFF_MANAGEMENT",
        ),
    ),
)
