"""
Preview Manager API

Router aggregator - imports all sub-routers and re-exports a single `router`.
"""

from fastapi import APIRouter

from app.routes import auth, base_files, cli, config, gitlab, previews, webhooks
from app import websockets

router = APIRouter()

router.include_router(auth.router)
router.include_router(base_files.router)
router.include_router(cli.router)
router.include_router(config.router)
router.include_router(gitlab.router)
router.include_router(previews.router)
router.include_router(webhooks.router)
router.include_router(websockets.router)
