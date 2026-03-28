"""Caddy Admin API manager — manage preview routing dynamically.

Strategy: The Caddyfile defines the wildcard *.mr.preview-mr.com route that
proxies all requests to the Python middleware. We patch the handlers of that
route via the Admin API to insert per-preview subroutes that proxy static
assets directly to the VM (bypassing Python), while HTML document requests
still go through forward_auth for authentication and then to the VM directly.

This way per-preview routes are evaluated BEFORE the default fallback handler,
all within the same wildcard route block.
"""

import json
import logging

import httpx

logger = logging.getLogger(__name__)

CADDY_ADMIN_URL = "http://localhost:2019"
ROUTES_PATH = "/config/apps/http/servers/srv0/routes"

# Static asset patterns that bypass auth and go directly to the VM
_STATIC_PATTERNS = [
    "*.css", "*.js", "*.map",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp", "*.avif",
    "*.woff", "*.woff2", "*.ttf", "*.eot", "*.otf",
    "*.json", "*.webmanifest", "*.xml", "*.txt",
    "*.mp4", "*.webm", "*.mp3", "*.pdf",
    "/sites/default/files/*",
    "/themes/*", "/modules/*", "/core/*", "/libraries/*",
]


class CaddyRouteManager:
    """Manage Caddy reverse proxy routes via the Admin API.

    Maintains an in-memory map of preview domains -> VM IPs and rebuilds
    the wildcard route's subroute list whenever previews change.
    """

    def __init__(self):
        self._preview_upstreams: dict[str, tuple[str, int]] = {}
        self._domain_public_paths: dict[str, list[str]] = {}
        self._wildcard_route_index: int | None = None

    def _find_wildcard_route_index(self, routes: list[dict]) -> int | None:
        """Find the index of the *.mr.preview-mr.com route."""
        for i, r in enumerate(routes):
            for m in r.get("match", []):
                if "*.mr.preview-mr.com" in m.get("host", []):
                    return i
        return None

    def _build_upstream_proxy(self, dial: str, port: int) -> dict:
        """Build a reverse_proxy handler for the upstream VM."""
        return {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": dial}],
        }

    def _build_preview_subroute(self, domain: str, upstream_ip: str, port: int) -> dict:
        """Build a subroute for a specific preview domain.

        Static assets -> direct to VM (no auth).
        Public paths -> direct to VM (no auth, for OAuth/API endpoints).
        HTML documents -> forward_auth check -> direct to VM.
        """
        dial = f"{upstream_ip}:{port}"
        proxy_handler = self._build_upstream_proxy(dial, port)
        terminal_dial = f"{upstream_ip}:8022"
        public_paths = self._domain_public_paths.get(domain, [])

        routes = [
            # Terminal WebSocket: proxy to terminal server on VM port 8022
            # Auth is handled by the terminal server itself via HMAC token
            {
                "match": [{"path": ["/_terminal/*"]}],
                "handle": [
                    {
                        "handler": "rewrite",
                        "strip_path_prefix": "/_terminal",
                    },
                    {
                        "handler": "reverse_proxy",
                        "upstreams": [{"dial": terminal_dial}],
                    },
                ],
            },
            # Static assets: bypass auth, proxy directly to VM
            {
                "match": [{"path": _STATIC_PATTERNS}],
                "handle": [proxy_handler],
            },
        ]

        # Public paths: bypass auth, proxy directly to VM (e.g. OAuth endpoints)
        if public_paths:
            routes.append({
                "match": [{"path": public_paths}],
                "handle": [proxy_handler],
            })

        # HTML/other: forward_auth then proxy to VM.
        # We save the original URI in a header, rewrite to the
        # auth endpoint, and on 2xx restore the original URI
        # so the next handler proxies the real request to the VM.
        routes.append({
            "handle": [
                # Save original URI before rewrite
                {
                    "handler": "headers",
                    "request": {
                        "set": {
                            "X-Forwarded-Host": ["{http.request.host}"],
                            "X-Forwarded-Uri": ["{http.request.uri}"],
                            "X-Forwarded-Method": ["{http.request.method}"],
                        },
                    },
                },
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": "host.docker.internal:8000"}],
                    "rewrite": {
                        "method": "GET",
                        "uri": "/api/auth/verify-preview",
                    },
                    "handle_response": [
                        {
                            "match": {"status_code": [2]},
                            "routes": [{
                                "handle": [
                                    # Restore original method + URI
                                    {
                                        "handler": "rewrite",
                                        "method": "{http.request.header.X-Forwarded-Method}",
                                        "uri": "{http.request.header.X-Forwarded-Uri}",
                                    },
                                    # Proxy to VM
                                    proxy_handler,
                                ],
                            }],
                        },
                        {
                            "match": {"status_code": [3]},
                            "routes": [{"handle": [{
                                "handler": "static_response",
                                "status_code": "{http.reverse_proxy.status_code}",
                                "headers": {"Location": ["{http.reverse_proxy.header.Location}"]},
                            }]}],
                        },
                        {"routes": [{"handle": [{
                            "handler": "static_response",
                            "status_code": "{http.reverse_proxy.status_code}",
                            "body": "Access denied",
                        }]}]},
                    ],
                },
            ],
        })

        return {
            "match": [{"host": [domain]}],
            "handle": [{
                "handler": "subroute",
                "routes": routes,
            }],
        }

    def _build_wildcard_subroute_handlers(self) -> list[dict]:
        """Build the complete subroute list for the wildcard route.

        Returns a list of subroutes: one per preview + a fallback to Python.
        """
        routes = []

        # Per-preview subroutes (matched by host)
        for domain, (ip, port) in self._preview_upstreams.items():
            routes.append(self._build_preview_subroute(domain, ip, port))

        # Fallback: unknown/stopped previews go to Python middleware
        routes.append({
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": "host.docker.internal:8000"}],
            }],
        })

        return routes

    async def _apply_routes(self) -> None:
        """Patch the wildcard route's handlers with current preview subroutes."""
        subroutes = self._build_wildcard_subroute_handlers()

        new_handle = [{
            "handler": "subroute",
            "routes": subroutes,
        }]

        async with httpx.AsyncClient() as client:
            if self._wildcard_route_index is None:
                resp = await client.get(f"{CADDY_ADMIN_URL}{ROUTES_PATH}")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to get routes: {resp.status_code}")
                routes = resp.json()
                self._wildcard_route_index = self._find_wildcard_route_index(routes)
                if self._wildcard_route_index is None:
                    raise RuntimeError("Wildcard route *.mr.preview-mr.com not found in Caddy config")

            # Patch the handle array of the wildcard route
            path = f"{ROUTES_PATH}/{self._wildcard_route_index}/handle"
            resp = await client.patch(
                f"{CADDY_ADMIN_URL}{path}",
                content=json.dumps(new_handle),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                # Route index might have changed, reset and retry once
                self._wildcard_route_index = None
                raise RuntimeError(f"Failed to patch wildcard route: {resp.status_code} {resp.text}")

        n = len(self._preview_upstreams)
        logger.info(f"Applied Caddy routes: {n} preview(s) + wildcard fallback")

    async def add_preview_route(
        self, domain: str, upstream_ip: str, port: int = 80
    ) -> None:
        """Register a preview domain -> VM mapping and update Caddy."""
        self._preview_upstreams[domain] = (upstream_ip, port)
        await self._apply_routes()
        logger.info("Added Caddy route: %s -> %s:%d", domain, upstream_ip, port)

    async def remove_preview_route(self, domain: str) -> None:
        """Remove a preview domain mapping and update Caddy."""
        if domain in self._preview_upstreams:
            del self._preview_upstreams[domain]
            self._domain_public_paths.pop(domain, None)
            await self._apply_routes()
            logger.info("Removed Caddy route: %s", domain)
        else:
            logger.debug("Caddy route not found for %s (already removed?)", domain)

    async def update_preview_route(
        self, domain: str, new_ip: str, port: int = 80
    ) -> None:
        """Update the upstream IP for an existing route."""
        await self.add_preview_route(domain, new_ip, port)

    async def add_preview_routes(
        self,
        url_hash: str,
        upstream_ip: str,
        alias_domains: list[str] | None = None,
        expose_services: dict[str, int] | None = None,
        public_paths: list[str] | None = None,
    ) -> None:
        """Add all routes for a preview: main domain + aliases + exposed services."""
        domain = f"{url_hash}.mr.preview-mr.com"
        self._preview_upstreams[domain] = (upstream_ip, 80)

        if public_paths:
            self._domain_public_paths[domain] = public_paths

        if alias_domains:
            for alias in alias_domains:
                self._preview_upstreams[alias] = (upstream_ip, 80)
                if public_paths:
                    self._domain_public_paths[alias] = public_paths

        if expose_services:
            for svc_name, port in expose_services.items():
                svc_domain = f"{svc_name}--{domain}"
                self._preview_upstreams[svc_domain] = (upstream_ip, port)

        await self._apply_routes()

    async def remove_preview_routes(
        self,
        url_hash: str,
        alias_domains: list[str] | None = None,
        expose_services: dict[str, int] | None = None,
    ) -> None:
        """Remove all routes for a preview."""
        domain = f"{url_hash}.mr.preview-mr.com"
        self._preview_upstreams.pop(domain, None)
        self._domain_public_paths.pop(domain, None)

        if alias_domains:
            for alias in alias_domains:
                self._preview_upstreams.pop(alias, None)
                self._domain_public_paths.pop(alias, None)

        if expose_services:
            for svc_name in expose_services:
                svc_domain = f"{svc_name}--{domain}"
                self._preview_upstreams.pop(svc_domain, None)

        await self._apply_routes()


    def set_domain_public_paths(self, domain: str, paths: list[str]) -> None:
        """Set public paths for a domain (no Caddy apply, batch with _apply_routes)."""
        if paths:
            self._domain_public_paths[domain] = paths
        else:
            self._domain_public_paths.pop(domain, None)

    async def refresh_project_public_paths(
        self, org_id: int, project_slug: str, paths: list[str]
    ) -> None:
        """Update public_paths for all active previews of a project and rebuild Caddy."""
        from app.database import get_all_previews, compute_url_hash, get_organization_by_id

        org = await get_organization_by_id(org_id)
        if not org:
            return
        org_slug = org["slug"]

        previews = await get_all_previews(org_id=org_id)
        changed = False
        for p in previews:
            if p.get("project_slug") != project_slug or not p.get("vm_ip"):
                continue
            url_hash = p.get("url_hash") or compute_url_hash(
                org_slug, project_slug, p["preview_name"]
            )
            domain = f"{url_hash}.mr.preview-mr.com"
            if domain in self._preview_upstreams:
                self.set_domain_public_paths(domain, paths)
                changed = True

        if changed:
            await self._apply_routes()
            logger.info(
                "Refreshed public_paths for project %s: %s", project_slug, paths
            )


# Singleton
caddy_manager = CaddyRouteManager()
