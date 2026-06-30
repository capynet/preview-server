"""Shared PostgreSQL pool + low-level helpers for the database package."""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config.settings import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_url_hash(org_slug: str, project_slug: str, preview_name: str) -> str:
    """Compute a deterministic 8-char hash for preview URLs."""
    raw = f"{org_slug}-{project_slug}-{preview_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


async def init_pool():
    """Create the asyncpg connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
    )
    logger.info("PostgreSQL connection pool initialized")


async def close_pool():
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


async def get_pool() -> asyncpg.Pool:
    """Get the connection pool."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


def _row_to_dict(row: asyncpg.Record) -> dict:
    return dict(row)
