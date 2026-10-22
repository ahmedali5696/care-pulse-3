"""Closed Wave 0 boundary, not the later signed identity/authorization protocol."""

from hmac import compare_digest
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class ClosedServiceBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/v1" or path.startswith("/v1/")):
            await self.app(scope, receive, send)
            return
        keys = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"x-carepulse-service-key"
        ]
        config = scope["app"].state.service_config
        valid = len(keys) == 1 and compare_digest(keys[0], config.service_key.encode())
        status = 403 if valid else 401
        response = JSONResponse(
            {
                "error": {
                    "code": "FORBIDDEN" if valid else "UNAUTHENTICATED",
                    "message": "Operation unavailable"
                    if valid
                    else "Authentication required",
                    "request_id": str(uuid4()),
                }
            },
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
