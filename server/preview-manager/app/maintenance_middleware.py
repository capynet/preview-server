"""Maintenance middleware — reject mutating API calls while draining for a deploy.

The UI also disables mutating actions, but the UI can be stale or bypassed; this
is the real barrier that prevents writes racing an in-progress control-plane deploy
and leaving state inconsistent. Read-only requests (GET/HEAD/OPTIONS) always pass.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Mutating requests to these path prefixes are ALWAYS allowed, even in maintenance:
# webhooks (durable inbox must keep accepting), the maintenance toggle itself, and
# auth (an admin must be able to log in to clear maintenance).
_EXEMPT_PREFIXES = (
    "/api/webhooks",
    "/api/admin/maintenance",
    "/api/auth",
)

_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


class MaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in _MUTATING_METHODS:
            path = request.url.path
            # Only ever guard the control-plane API. Preview-app traffic (POSTs to
            # the Drupal sites on hash subdomains) must never be blocked by this.
            if path.startswith("/api/") and not any(path.startswith(p) for p in _EXEMPT_PREFIXES):
                from app.valkey import is_maintenance_active, get_maintenance
                if await is_maintenance_active():
                    state = await get_maintenance()
                    logger.info(f"Maintenance active — rejecting {request.method} {path} (503)")
                    return JSONResponse(
                        status_code=503,
                        content={"detail": "maintenance", "maintenance": state},
                    )
        return await call_next(request)
