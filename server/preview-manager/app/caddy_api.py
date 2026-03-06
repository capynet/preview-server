"""Caddy Admin API manager — add/remove routes dynamically."""

import json
import logging

import httpx

logger = logging.getLogger(__name__)

CADDY_ADMIN_URL = "http://localhost:2019"
ROUTES_PATH = "/config/apps/http/servers/srv0/routes"


class CaddyRouteManager:
    """Manage Caddy reverse proxy routes via the Admin API."""

    def _route_id(self, domain: str) -> str:
        """Deterministic route ID from domain."""
        return f"preview-{domain.replace('.', '-')}"

    def _build_route(self, domain: str, upstream_ip: str, port: int = 80) -> dict:
        """Build a Caddy route config for a preview."""
        route_id = self._route_id(domain)
        return {
            "@id": route_id,
            "match": [{"host": [domain]}],
            "handle": [
                {
                    "handler": "subroute",
                    "routes": [
                        # Forward auth for non-static paths
                        {
                            "match": [{
                                "not": [{"path": [
                                    "*.css", "*.js", "*.map",
                                    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp", "*.avif",
                                    "*.woff", "*.woff2", "*.ttf", "*.eot", "*.otf",
                                    "*.json", "*.webmanifest", "*.xml", "*.txt",
                                    "/sites/default/files/*",
                                ]}]
                            }],
                            "handle": [{
                                "handler": "forward_auth",
                                "uri": "/api/auth/verify-preview",
                                "upstreams": [{"dial": "host.docker.internal:8000"}],
                                "headers": {"up": {"Host": ["{http.request.host}"]}},
                            }],
                        },
                        # Reverse proxy to VM
                        {
                            "handle": [{
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": f"{upstream_ip}:{port}"}],
                            }],
                        },
                    ],
                },
            ],
            "terminal": True,
        }

    async def add_preview_route(
        self, domain: str, upstream_ip: str, port: int = 80
    ) -> None:
        """Add a route for a preview domain pointing to a VM IP."""
        route = self._build_route(domain, upstream_ip, port)
        route_id = self._route_id(domain)

        async with httpx.AsyncClient() as client:
            # Try to update existing route first
            resp = await client.get(
                f"{CADDY_ADMIN_URL}/id/{route_id}",
            )
            if resp.status_code == 200:
                # Route exists, replace it
                resp = await client.patch(
                    f"{CADDY_ADMIN_URL}/id/{route_id}",
                    content=json.dumps(route),
                    headers={"Content-Type": "application/json"},
                )
            else:
                # Prepend new route (before wildcard fallback)
                resp = await client.post(
                    f"{CADDY_ADMIN_URL}{ROUTES_PATH}/0",
                    content=json.dumps(route),
                    headers={"Content-Type": "application/json"},
                )

            if resp.status_code not in (200, 201):
                logger.error(
                    "Failed to add Caddy route for %s: %d %s",
                    domain, resp.status_code, resp.text,
                )
                raise RuntimeError(f"Caddy API error: {resp.status_code} {resp.text}")

        logger.info("Added Caddy route: %s -> %s:%d", domain, upstream_ip, port)

    async def remove_preview_route(self, domain: str) -> None:
        """Remove a preview route from Caddy."""
        route_id = self._route_id(domain)
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{CADDY_ADMIN_URL}/id/{route_id}")
            if resp.status_code == 200:
                logger.info("Removed Caddy route: %s", domain)
            elif resp.status_code == 404:
                logger.debug("Caddy route not found for %s (already removed?)", domain)
            else:
                logger.warning(
                    "Failed to remove Caddy route for %s: %d %s",
                    domain, resp.status_code, resp.text,
                )

    async def update_preview_route(
        self, domain: str, new_ip: str, port: int = 80
    ) -> None:
        """Update the upstream IP for an existing route."""
        await self.add_preview_route(domain, new_ip, port)

    async def add_preview_routes(
        self,
        preview_name: str,
        project_name: str,
        upstream_ip: str,
        alias_domains: list[str] | None = None,
        expose_services: dict[str, int] | None = None,
    ) -> None:
        """Add all routes for a preview: main domain + aliases + exposed services."""
        domain = f"{preview_name}-{project_name}.mr.preview-mr.com"
        await self.add_preview_route(domain, upstream_ip)

        if alias_domains:
            for alias in alias_domains:
                await self.add_preview_route(alias, upstream_ip)

        if expose_services:
            for svc_name, port in expose_services.items():
                svc_domain = f"{svc_name}--{domain}"
                await self.add_preview_route(svc_domain, upstream_ip, port)

    async def remove_preview_routes(
        self,
        preview_name: str,
        project_name: str,
        alias_domains: list[str] | None = None,
        expose_services: dict[str, int] | None = None,
    ) -> None:
        """Remove all routes for a preview."""
        domain = f"{preview_name}-{project_name}.mr.preview-mr.com"
        await self.remove_preview_route(domain)

        if alias_domains:
            for alias in alias_domains:
                await self.remove_preview_route(alias)

        if expose_services:
            for svc_name in expose_services:
                svc_domain = f"{svc_name}--{domain}"
                await self.remove_preview_route(svc_domain)


# Singleton
caddy_manager = CaddyRouteManager()
