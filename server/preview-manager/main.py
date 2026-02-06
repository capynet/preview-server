#!/usr/bin/env python3
"""
Preview Manager

Simple preview deployment system for Drupal environments with DDEV.
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
    from app.auth.database import init_db
    logger.info("Starting Preview Manager Service")
    await init_db()
    logger.info("Preview Manager Service started successfully")

    yield

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
        "https://www.preview-mr.com",
        "https://preview-mr.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
