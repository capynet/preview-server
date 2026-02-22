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
    from app.database import init_db
    from app.config_store import load_config_to_settings
    from app.tasks.auto_stop import auto_stop_loop
    from app.tasks.auto_erase import auto_erase_loop
    from app.tasks.docker_events import docker_events_loop
    from app.websockets import system_resources_loop

    logger.info("Starting Preview Manager Service")
    await init_db()
    await load_config_to_settings()

    # Start background tasks
    auto_stop_task = asyncio.create_task(auto_stop_loop())
    auto_erase_task = asyncio.create_task(auto_erase_loop())
    docker_events_task = asyncio.create_task(docker_events_loop())
    system_resources_task = asyncio.create_task(system_resources_loop())
    logger.info("Preview Manager Service started successfully")

    yield

    # Cancel background tasks
    for task in (auto_stop_task, auto_erase_task, docker_events_task, system_resources_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down Preview Manager Service")
    logger.info("Preview Manager Service stopped")


def handle_signal(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    sys.exit(0)


app = FastAPI(
    title="Preview Manager",
    description="Preview deployment system for Drupal environments",
    version="2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        "https://app.preview-mr.com",
        "https://www.preview-mr.com",
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


def main():
    """Main application entry point"""
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        access_log=True
    )


if __name__ == "__main__":
    main()
