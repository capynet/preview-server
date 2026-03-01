"""Middleware to wake up stopped previews when accessed via browser."""

import asyncio
import logging
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from app.auth.dependencies import SESSION_COOKIE
from app.auth import database as auth_db
from app.database import get_preview_by_domain, update_last_accessed, has_running_deployment

logger = logging.getLogger(__name__)

# Track previews currently being woken up to avoid duplicate starts
_waking_up: set[str] = set()

WAKE_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="5">
    <title>Waking up preview...</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background: #0a0a0a;
            color: #e5e5e5;
        }}
        .container {{
            text-align: center;
            max-width: 480px;
            padding: 2rem;
        }}
        .spinner {{
            width: 48px;
            height: 48px;
            border: 4px solid #333;
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 1.5rem;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        h1 {{
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }}
        p {{
            color: #888;
            font-size: 0.9rem;
            line-height: 1.5;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="spinner"></div>
        <h1>Waking up preview</h1>
        <p>{preview_name} &mdash; {project}</p>
        <p>This page will refresh automatically...</p>
    </div>
</body>
</html>"""


BUILDING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="10">
    <title>Building preview...</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background: #0a0a0a;
            color: #e5e5e5;
        }}
        .container {{
            text-align: center;
            max-width: 480px;
            padding: 2rem;
        }}
        .spinner {{
            width: 48px;
            height: 48px;
            border: 4px solid #333;
            border-top-color: #f59e0b;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 1.5rem;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        h1 {{
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }}
        p {{
            color: #888;
            font-size: 0.9rem;
            line-height: 1.5;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="spinner"></div>
        <h1>Building preview</h1>
        <p>{preview_name} &mdash; {project}</p>
        <p>A deployment is in progress. This page will refresh automatically when it&rsquo;s ready.</p>
    </div>
</body>
</html>"""


class WakePreviewMiddleware(BaseHTTPMiddleware):
    """Intercept requests to *.mr.preview-mr.com that hit the API fallback.

    When Caddy has no specific route for a preview domain (container stopped),
    the wildcard fallback proxies the request here. We check the DB, wake the
    preview, and return a waiting page that auto-refreshes.
    """

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")

        # Only handle preview domain requests (from Caddy wildcard fallback)
        if not host.endswith(".mr.preview-mr.com"):
            return await call_next(request)

        # Check authentication
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return self._redirect_to_login(host, request)

        session = await auth_db.get_session(session_id)
        if not session:
            return self._redirect_to_login(host, request)

        # Look up preview in DB
        preview = await get_preview_by_domain(host)
        if not preview:
            return HTMLResponse(
                content="<h1>Preview not found</h1><p>No preview matches this URL.</p>",
                status_code=404,
            )

        project = preview["project"]
        preview_name = preview["preview_name"]
        preview_path = Path(preview["path"]) if preview.get("path") else None

        # Check if a deployment is running — show building page instead of waking
        if preview.get("id"):
            try:
                building = await has_running_deployment(preview["id"])
                if building:
                    return HTMLResponse(
                        content=BUILDING_PAGE_HTML.format(
                            preview_name=preview_name,
                            project=project,
                        ),
                        status_code=200,
                    )
            except Exception:
                pass

        if not preview_path or not preview_path.exists():
            return HTMLResponse(
                content="<h1>Preview not found</h1><p>Preview directory does not exist.</p>",
                status_code=404,
            )

        # Start containers in background (if not already waking)
        wake_key = f"{project}/{preview_name}"
        if wake_key not in _waking_up:
            _waking_up.add(wake_key)
            asyncio.create_task(self._wake_containers(wake_key, preview_path, project, preview_name))

        # Update last_accessed_at
        try:
            await update_last_accessed(project, preview_name)
        except Exception:
            pass

        return HTMLResponse(
            content=WAKE_PAGE_HTML.format(preview_name=preview_name, project=project),
            status_code=200,
        )

    @staticmethod
    async def _wake_containers(wake_key: str, preview_path: Path, project: str, preview_name: str):
        """Run docker compose up -d in the background."""
        try:
            logger.info(f"Waking up {project}/{preview_name}")
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "up", "-d",
                cwd=str(preview_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                logger.info(f"Woke up {project}/{preview_name} successfully")
            else:
                logger.error(f"Failed to wake {project}/{preview_name}: {stderr.decode()}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout waking {project}/{preview_name}")
        except Exception as e:
            logger.error(f"Error waking {project}/{preview_name}: {e}")
        finally:
            _waking_up.discard(wake_key)

    @staticmethod
    def _redirect_to_login(host: str, request: Request) -> RedirectResponse:
        original_url = f"https://{host}{request.url.path}"
        login_url = f"{settings.frontend_url}/auth/login?redirect_to={original_url}"
        return RedirectResponse(login_url, status_code=302)
