"""Foundation-only ASGI entrypoint. Business routes are not enabled in Wave 0."""

from fastapi import FastAPI

app = FastAPI(
    title="CarePulse Foundation",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
