#!/usr/bin/env python3
"""
Preview Manager

Simple preview deployment system for Drupal environments with Docker Compose.
"""

import logging
import signal
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    import asyncio
    from app.database import init_pool, close_pool
    from app.valkey import init_valkey, close_valkey
    from app.config_store import load_config_to_settings
    from app.websockets import system_resources_loop

    logger.info("Starting Preview Manager Service")

    # Initialize PostgreSQL pool
    await init_pool()

    # Initialize Valkey
    await init_valkey()

    await load_config_to_settings()

    # Initialize arq pool for task enqueueing
    from arq import create_pool as create_arq_pool
    from arq.connections import RedisSettings
    arq_pool = await create_arq_pool(RedisSettings.from_dsn(settings.valkey_url))
    app.state.arq = arq_pool
    logger.info("arq pool initialized")

    # Set up Caddy routes for active previews
    from app.caddy_api import caddy_manager
    from app.database import get_previews_with_active_vms, compute_url_hash
    try:
        active = await get_previews_with_active_vms()
        for p in active:
            if p.get("vm_ip"):
                org_slug = p.get("org_slug", "")
                project_slug = p.get("project_slug", "")
                preview_name = p["preview_name"]
                url_hash = p.get("url_hash") or compute_url_hash(org_slug, project_slug, preview_name)
                domain = f"{url_hash}.mr.preview-mr.com"
                caddy_manager._preview_upstreams[domain] = (p["vm_ip"], 80)
        await caddy_manager._apply_routes()
        logger.info(f"Caddy routes ready: {len(active)} preview(s) + wildcard fallback")
    except Exception as e:
        logger.warning(f"Failed to set up Caddy routes: {e}")

    # System resources monitor still runs in-process (lightweight, 2s interval)
    system_resources_task = asyncio.create_task(system_resources_loop())
    logger.info("Preview Manager Service started successfully")

    yield

    # Shutdown
    system_resources_task.cancel()
    try:
        await system_resources_task
    except asyncio.CancelledError:
        pass

    if hasattr(app.state, "arq") and app.state.arq:
        await app.state.arq.close()
    await close_valkey()
    await close_pool()

    logger.info("Preview Manager Service stopped")


def handle_signal(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    sys.exit(0)


app = FastAPI(
    title="Preview Manager",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        "https://app.preview-mr.com",
        "https://preview-mr.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.wake_preview import WakePreviewMiddleware
app.add_middleware(WakePreviewMiddleware)

from app.api import router
app.include_router(router)

app.router.lifespan_context = lifespan


def run_migrations():
    """Run Alembic migrations once before forking workers."""
    try:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations applied")
    except Exception as e:
        logger.warning(f"Alembic migration error (may be first run): {e}")


def main():
    """Main application entry point"""
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run migrations once in the parent process before forking workers
    run_migrations()

    # Seed default data (only if DB is empty)
    from seed import seed_database
    seed_database()

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        workers=settings.uvicorn_workers,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
