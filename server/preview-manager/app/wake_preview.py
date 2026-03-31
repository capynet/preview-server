"""Middleware to handle preview domain requests — proxy to VM or wake up stopped previews."""

import asyncio
import logging

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response

from config.settings import settings
from app.auth.dependencies import SESSION_COOKIE
from app.auth import database as auth_db
from app.database import (
    get_preview_by_domain, update_last_accessed, has_running_deployment,
    update_preview_vm, compute_url_hash,
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
        <p>Powering on VM... This page will refresh automatically (~30-40s).</p>
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
    """Handle all *.{preview_domain} requests.

    The Caddy wildcard proxies ALL preview subdomain requests here.
    This middleware:
    - Checks auth
    - If VM running → reverse proxy to VM
    - If VM stopped → power on + show waiting page
    - If deploying → show building page
    - If no preview → 404
    """

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")

        # Only handle preview domain requests (from Caddy wildcard)
        # Exclude the API and root domain (they share the same base domain)
        if not host.endswith(f".{settings.preview_domain}"):
            return await call_next(request)
        if host in (f"api.{settings.base_domain}", settings.base_domain):
            return await call_next(request)

        # Forward auth requests from Caddy — let FastAPI handle them directly
        if request.url.path == "/api/auth/verify-preview":
            return await call_next(request)

        # Skip auth for static assets — only HTML documents need auth
        path = request.url.path
        _STATIC_PREFIXES = (
            "/sites/default/files/",
            "/themes/",
            "/modules/",
            "/core/",
            "/libraries/",
        )
        _STATIC_EXTENSIONS = (
            ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
            ".woff", ".woff2", ".ttf", ".eot", ".otf",
            ".json", ".xml", ".txt", ".map", ".webp", ".avif",
            ".mp4", ".webm", ".mp3", ".pdf",
        )
        skip_auth = (
            any(path.startswith(p) for p in _STATIC_PREFIXES)
            or path.lower().endswith(_STATIC_EXTENSIONS)
        )

        if not skip_auth:
            # Check authentication
            session_id = request.cookies.get(SESSION_COOKIE)
            if not session_id:
                return self._redirect_to_login(host, request)

            session = await auth_db.get_session(session_id)
            if not session:
                return self._redirect_to_login(host, request)

        # Look up preview in DB (hash-based domain resolution)
        preview = await get_preview_by_domain(host)
        if not preview:
            return HTMLResponse(
                content="<h1>Preview not found</h1><p>No preview matches this URL.</p>",
                status_code=404,
            )

        project_slug = preview.get("project_slug", "")
        preview_name = preview["preview_name"]

        # Update last_accessed_at (fire and forget, only for authenticated requests)
        try:
            await update_last_accessed(preview["id"])
        except Exception:
            pass

        # Check if a deployment is running — show building page
        if preview.get("id"):
            try:
                building = await has_running_deployment(preview["id"])
                if building:
                    return HTMLResponse(
                        content=BUILDING_PAGE_HTML.format(
                            preview_name=preview_name,
                            project=project_slug,
                        ),
                        status_code=200,
                    )
            except Exception:
                pass

        # If VM is running, reverse proxy to it
        if preview.get("vm_id") and preview.get("vm_ip"):
            # Detect exposed service domain (e.g. solr--hash.{preview_domain})
            import re
            port = 80
            escaped_domain = re.escape(settings.preview_domain)
            match = re.match(rf"^(.+?)\.{escaped_domain}$", host)
            if match:
                subdomain = match.group(1)
                if "--" in subdomain:
                    svc_prefix = subdomain.split("--")[0]
                    # Look up exposed service port from DB
                    import json
                    expose_raw = preview.get("expose_config", "{}")
                    try:
                        expose = json.loads(expose_raw) if expose_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        expose = {}
                    if svc_prefix in expose:
                        port = int(expose[svc_prefix])
            return await self._proxy_to_vm(request, preview["vm_ip"], host, port=port)

        # No VM means preview was deleted or never created
        if not preview.get("vm_id"):
            return HTMLResponse(
                content="<h1>Preview not available</h1><p>This preview has been deleted. Use rebuild to recreate it.</p>",
                status_code=404,
            )

        # VM exists but no IP — shouldn't happen, but handle gracefully
        return HTMLResponse(
            content="<h1>Preview starting</h1><p>Please wait...</p>",
            status_code=503,
        )

    @staticmethod
    async def _proxy_to_vm(
        request: Request, vm_ip: str, host: str, *, port: int = 80,
    ) -> Response:
        """Reverse proxy the request to the preview VM."""
        url = f"http://{vm_ip}:{port}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        # Forward relevant headers
        headers = {}
        for key, value in request.headers.items():
            key_lower = key.lower()
            if key_lower in ("host", "connection", "transfer-encoding"):
                continue
            headers[key] = value
        headers["Host"] = host
        headers["X-Forwarded-For"] = request.client.host if request.client else ""
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Real-IP"] = request.client.host if request.client else ""

        body = await request.body()

        try:
            client = httpx.AsyncClient(timeout=60.0)
            req = client.build_request(
                method=request.method,
                url=url,
                headers=headers,
                content=body if body else None,
            )
            resp = await client.send(req, stream=True)

            # Filter hop-by-hop headers from response
            resp_headers = {}
            for key, value in resp.headers.items():
                if key.lower() not in (
                    "transfer-encoding", "connection", "keep-alive",
                    "proxy-authenticate", "proxy-authorization", "te",
                    "trailers", "upgrade",
                ):
                    resp_headers[key] = value

            async def stream():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream(),
                status_code=resp.status_code,
                headers=resp_headers,
            )
        except httpx.ConnectError:
            return HTMLResponse(
                content="<h1>Preview unavailable</h1><p>VM is not responding. It may still be starting up. Try refreshing.</p>",
                status_code=502,
            )
        except Exception as e:
            logger.error(f"Proxy error to {vm_ip}: {e}")
            return HTMLResponse(
                content=f"<h1>Proxy error</h1><p>{e}</p>",
                status_code=502,
            )

    @staticmethod
    async def _wake_cloud_preview(
        wake_key: str, org_slug: str, project_slug: str, preview_name: str, preview: dict
    ):
        """Power on a shutdown VM and start containers."""
        from app.cloud import cloud_manager
        from app.remote import RemoteExecutor
        from app.deployment import VM_PREVIEW_DIR

        try:
            logger.info(f"Waking up cloud preview {org_slug}/{project_slug}/{preview_name}")
            vm_id = preview["vm_id"]

            # Power on the VM
            vm_ip = await cloud_manager.power_on_vm(vm_id)
            await cloud_manager.wait_for_vm_ready(vm_id, timeout=120)

            # Start containers
            executor = RemoteExecutor(vm_ip)
            proc = await executor.run_shell(
                f"cd {VM_PREVIEW_DIR}/code && docker compose up -d"
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                await update_preview_vm(preview["id"], vm_id, vm_ip)

                # Activate Redis cache backend (flag file is lost on VM reboot)
                url_hash = preview.get("url_hash", "")
                php_container = f"{url_hash}-php"
                touch_proc = await executor.run_shell(
                    f"docker exec {php_container} touch /tmp/.preview_redis_ready 2>/dev/null"
                )
                await touch_proc.communicate()

                # Re-register Caddy routes (main + expose services)
                from app.caddy_api import caddy_manager
                from app.database import get_project
                url_hash = compute_url_hash(org_slug, project_slug, preview_name)
                try:
                    import json
                    expose_raw = preview.get("expose_config", "{}")
                    expose = json.loads(expose_raw) if expose_raw else {}
                    # Fetch project public_paths for Caddy bypass
                    public_paths = None
                    proj = await get_project(preview["project_id"])
                    if proj and proj.get("public_paths"):
                        try:
                            public_paths = json.loads(proj["public_paths"]) or None
                        except (ValueError, TypeError):
                            pass
                    await caddy_manager.add_preview_routes(
                        url_hash, vm_ip,
                        expose_services=expose or None,
                        public_paths=public_paths,
                    )
                except Exception as e:
                    logger.warning(f"Failed to add Caddy route after wake: {e}")
                logger.info(f"Woke up {org_slug}/{project_slug}/{preview_name} (VM {vm_id}, IP {vm_ip})")
            else:
                logger.error(
                    f"Failed to start containers for {project_slug}/{preview_name}: "
                    f"{stderr.decode()}"
                )

        except Exception as e:
            logger.error(f"Error waking cloud preview {project_slug}/{preview_name}: {e}")
        finally:
            _waking_up.discard(wake_key)

    @staticmethod
    def _redirect_to_login(host: str, request: Request) -> RedirectResponse:
        original_url = f"https://{host}{request.url.path}"
        login_url = f"{settings.frontend_url}/auth/login?redirect_to={original_url}"
        return RedirectResponse(login_url, status_code=302)
