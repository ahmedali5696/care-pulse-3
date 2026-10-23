"""Private API entrypoint; register no clinical routes until their owning tasks."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
app.add_exception_handler(ContractError, contract_exception_handler)
app.add_exception_handler(SQLAlchemyError, lambda _request, _error: error_response(503))
app.add_exception_handler(
    ReceiptConflict,
    lambda _request, _error: error_response(409, code="IDEMPOTENCY_CONFLICT"),
)
app.add_middleware(ClosedServiceBoundary)
