"""Foundation-only ASGI entrypoint. Business routes are not enabled in Wave 0."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from carepulse.config import load_config
from carepulse.db import create_db_engine


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
