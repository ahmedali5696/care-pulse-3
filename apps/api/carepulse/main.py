"""Private signed API entrypoint with explicit purpose-bound route registration."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from re import compile

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from carepulse.appointments.intake_routes import router as intake_router
from carepulse.appointments.routes import router as appointment_router
from carepulse.config import load_config
from carepulse.db import create_db_engine, create_gate_engine
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
from carepulse.policies.admission_routes import router as admission_router
from carepulse.policies.pricing_routes import router as pricing_router
from carepulse.policies.queue_routes import router as queue_policy_router
from carepulse.policies.schedule_routes import router as schedule_router
from carepulse.policies.slots_routes import router as slots_router
from carepulse.provisioning.routes import router as provisioning_router
from carepulse.queue.routes import router as queue_router
from carepulse.visits.clinical_contracts import signed_visit_patch_purpose
from carepulse.visits.routes import router as visit_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Refuse protected startup before initializing any operational capability."""
    app.state.service_config = load_config("api")
    app.state.db_engine = create_db_engine(app.state.service_config)
    app.state.gate_engine = create_gate_engine(app.state.service_config)
    try:
        yield
    finally:
        app.state.db_engine.dispose()
        app.state.gate_engine.dispose()


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
app.include_router(appointment_router)
app.include_router(intake_router)
app.include_router(visit_router)
app.include_router(pricing_router)
app.include_router(schedule_router)
app.include_router(slots_router)
app.include_router(queue_router)
app.include_router(admission_router)
app.include_router(queue_policy_router)
app.add_exception_handler(ContractError, contract_exception_handler)
app.add_exception_handler(
    RequestValidationError, lambda _request, _error: error_response(422)
)


def _http_exception_handler(request, error: StarletteHTTPException):
    malformed_json_routes = {
        ("PATCH", "/v1/settings/pricing"),
        ("PATCH", "/v1/settings/schedule"),
        ("POST", "/v1/settings/schedule/preview"),
        ("POST", "/v1/settings/schedule/activate"),
    }
    if (
        error.status_code == 400
        and (request.method, request.url.path) in malformed_json_routes
    ):
        return error_response(422)
    return JSONResponse(
        {"detail": error.detail}, status_code=error.status_code, headers=error.headers
    )


app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
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
        ("GET", "/v1/appointments"): "APPOINTMENT_BOOKING",
        ("POST", "/v1/appointments"): "APPOINTMENT_BOOKING",
        ("POST", "/v1/intake/routine"): "INTAKE_ROUTINE",
        ("POST", "/v1/intake/emergency"): "INTAKE_EMERGENCY",
        ("POST", "/v1/intake/urgent"): "INTAKE_URGENT",
        ("GET", "/v1/settings/pricing"): "SETTINGS_PRICING",
        ("PATCH", "/v1/settings/pricing"): "SETTINGS_PRICING",
        ("GET", "/v1/settings/schedule"): "SETTINGS_SCHEDULE",
        ("PATCH", "/v1/settings/schedule"): "SETTINGS_SCHEDULE",
        ("POST", "/v1/settings/schedule/preview"): "SETTINGS_SCHEDULE",
        ("POST", "/v1/settings/schedule/activate"): "SETTINGS_SCHEDULE",
        ("GET", "/v1/slots"): "SLOTS",
        ("GET", "/v1/queue"): "QUEUE_READ",
        ("GET", "/v1/settings/admission"): "ADMISSION_SETTINGS_READ",
        ("PATCH", "/v1/settings/admission"): "ADMISSION_MODE_UPDATE",
        ("PATCH", "/v1/queue/capacity"): "QUEUE_CAPACITY_UPDATE",
        ("POST", "/v1/queue/pause"): "QUEUE_PAUSE",
        ("POST", "/v1/queue/admit"): "QUEUE_ADMIT",
        ("GET", "/v1/settings/queue"): "SETTINGS_QUEUE_READ",
        ("PATCH", "/v1/settings/queue"): "SETTINGS_QUEUE_UPDATE",
        ("POST", "/v1/queue/placement"): "QUEUE_PLACEMENT",
        ("POST", "/v1/queue/admission-confirmations"): "QUEUE_CONFIRMATION_ISSUE",
        (
            "GET",
            "/v1/queue/admission-confirmations/usable",
        ): "QUEUE_CONFIRMATION_USABLE",
    },
    route_patterns=(
        (
            "POST",
            compile(
                r"/v1/queue/admission-confirmations/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/revoke"
            ),
            "QUEUE_CONFIRMATION_REVOKE",
        ),
        (
            "POST",
            compile(
                r"/v1/appointments/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/check-in"
            ),
            "QUEUE_CHECK_IN",
        ),
        (
            "POST",
            compile(
                r"/v1/appointments/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/cancel"
            ),
            "APPOINTMENT_CANCEL",
        ),
        (
            "POST",
            compile(
                r"/v1/appointments/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/replace-urgent"
            ),
            "APPOINTMENT_REPLACE_URGENT",
        ),
        (
            "POST",
            compile(
                r"/v1/appointments/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/reschedule"
            ),
            "APPOINTMENT_RESCHEDULE",
        ),
        (
            "PATCH",
            compile(r"/v1/visits/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"),
            signed_visit_patch_purpose,
        ),
        (
            "GET",
            compile(r"/v1/visits/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"),
            "VISIT_READ",
        ),
        (
            "POST",
            compile(
                r"/v1/visits/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/complete"
            ),
            "VISIT_COMPLETE",
        ),
        (
            "POST",
            compile(
                r"/v1/visits/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/cancel-exceptional"
            ),
            "VISIT_CANCEL_EXCEPTIONAL",
        ),
        (
            "POST",
            compile(
                r"/v1/visits/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/extend-followup"
            ),
            "VISIT_FOLLOWUP",
        ),
        (
            "GET",
            compile(
                r"/v1/appointments/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
            ),
            "APPOINTMENT_BOOKING",
        ),
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
