"""Middleware to wake up stopped cloud previews when accessed via browser."""

import asyncio
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from app.auth.dependencies import SESSION_COOKIE
from app.auth import database as auth_db
from app.database import (
    get_preview_by_domain, update_last_accessed, has_running_deployment,
    update_preview_vm,
)

logger = logging.getLogger(__name__)

# Track previews currently being woken up to avoid duplicate starts
_waking_up: set[str] = set()

WAKE_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="15">
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
        <p>Starting a new VM... This page will refresh automatically (~30-40s).</p>
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

    When Caddy has no specific route for a preview domain (VM destroyed),
    the wildcard fallback proxies the request here. We check the DB,
    create a new VM + attach existing volume, and return a waiting page.
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

        # Check if a deployment is running — show building page
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

        # If VM is already running, something else is wrong — show error
        if preview.get("vm_id") and preview.get("vm_ip"):
            return HTMLResponse(
                content="<h1>Preview error</h1><p>VM is running but route is missing. Try refreshing in a few seconds.</p>",
                status_code=503,
            )

        # No volume means preview was fully deleted
        if not preview.get("volume_id"):
            return HTMLResponse(
                content="<h1>Preview not available</h1><p>This preview has been fully deleted.</p>",
                status_code=404,
            )

        # Start VM in background (if not already waking)
        wake_key = f"{project}/{preview_name}"
        if wake_key not in _waking_up:
            _waking_up.add(wake_key)
            asyncio.create_task(
                self._wake_cloud_preview(wake_key, project, preview_name, preview)
            )

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
    async def _wake_cloud_preview(
        wake_key: str, project: str, preview_name: str, preview: dict
    ):
        """Create a new VM, attach the existing volume, start containers."""
        from app.cloud import cloud_manager
        from app.caddy_api import caddy_manager
        from app.remote import RemoteExecutor

        try:
            logger.info(f"Waking up cloud preview {project}/{preview_name}")
            volume_id = preview["volume_id"]

            # Create VM with volume attached
            vm_name = f"prev-{project}-{preview_name}"
            server = await cloud_manager.create_vm(vm_name, volume_id)
            vm_id = server.data_model.id
            vm_ip = server.data_model.public_net.ipv4.ip

            # Wait for SSH
            executor = RemoteExecutor(vm_ip)
            await executor.wait_for_ssh(timeout=120)

            # Mount volume and start containers
            setup_cmd = (
                "VOLDIR=$(ls -d /mnt/HC_Volume* 2>/dev/null | head -1) && "
                "if [ -z \"$VOLDIR\" ]; then echo 'No volume found' >&2; exit 1; fi && "
                "ln -sfn \"$VOLDIR\" /var/www/preview && "
                "cd /var/www/preview/code && "
                "docker compose up -d"
            )
            proc = await executor.run_shell(setup_cmd)
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # Update DB with VM info
                await update_preview_vm(project, preview_name, vm_id, vm_ip)

                # Add Caddy route
                await caddy_manager.add_preview_routes(
                    preview_name, project, vm_ip
                )

                logger.info(f"Woke up {project}/{preview_name} (VM {vm_id}, IP {vm_ip})")
            else:
                logger.error(
                    f"Failed to start containers for {project}/{preview_name}: "
                    f"{stderr.decode()}"
                )
                # Clean up VM since containers failed
                await cloud_manager.destroy_vm(vm_id)

        except Exception as e:
            logger.error(f"Error waking cloud preview {project}/{preview_name}: {e}")
        finally:
            _waking_up.discard(wake_key)

    @staticmethod
    def _redirect_to_login(host: str, request: Request) -> RedirectResponse:
        original_url = f"https://{host}{request.url.path}"
        login_url = f"{settings.frontend_url}/auth/login?redirect_to={original_url}"
        return RedirectResponse(login_url, status_code=302)
