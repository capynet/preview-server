"""
Preview Manager API

Router aggregator - imports all sub-routers and re-exports a single `router`.
"""

from fastapi import APIRouter

from app.routes import auth, config, deploy, gitlab, previews
from app import websockets

router = APIRouter()

router.include_router(auth.router)
router.include_router(config.router)
router.include_router(deploy.router)
router.include_router(gitlab.router)
router.include_router(previews.router)
router.include_router(websockets.router)
