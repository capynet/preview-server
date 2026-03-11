"""Valkey (Redis-compatible) connection and pub/sub helpers."""

import json
import logging
from typing import Optional

import redis.asyncio as redis

from config.settings import settings

logger = logging.getLogger(__name__)

_valkey: Optional[redis.Redis] = None


async def init_valkey():
    """Initialize Valkey connection."""
    global _valkey
    _valkey = redis.from_url(settings.valkey_url, decode_responses=True)
    # Test connection
    await _valkey.ping()
    logger.info("Valkey connection initialized")


async def close_valkey():
    """Close Valkey connection."""
    global _valkey
    if _valkey:
        await _valkey.close()
        _valkey = None
        logger.info("Valkey connection closed")


def get_valkey() -> redis.Redis:
    """Get the Valkey client."""
    if _valkey is None:
        raise RuntimeError("Valkey not initialized. Call init_valkey() first.")
    return _valkey


async def publish_event(channel: str, data: dict):
    """Publish an event to a Valkey pub/sub channel."""
    v = get_valkey()
    await v.publish(channel, json.dumps(data))


async def subscribe(channel: str) -> redis.client.PubSub:
    """Subscribe to a channel. Returns a PubSub instance."""
    v = get_valkey()
    pubsub = v.pubsub()
    await pubsub.subscribe(channel)
    return pubsub


# ---- Deployment log buffer ----

async def buffer_deploy_log(deployment_id: int, line: str):
    """Append a log line to the deployment buffer in Valkey."""
    v = get_valkey()
    key = f"deploy_logs:{deployment_id}"
    await v.rpush(key, line)
    await v.expire(key, 3600)  # TTL 1 hour


async def get_deploy_log_buffer(deployment_id: int) -> list[str]:
    """Get all buffered log lines for a deployment."""
    v = get_valkey()
    key = f"deploy_logs:{deployment_id}"
    return await v.lrange(key, 0, -1)



# ---- Deployment status tracking ----

async def mark_deploy_complete(deployment_id: int, success: bool):
    """Mark a deployment as complete in Valkey."""
    v = get_valkey()
    await v.set(f"deploy_complete:{deployment_id}", "1" if success else "0", ex=3600)


async def get_deploy_complete(deployment_id: int) -> bool | None:
    """Check if deployment is complete. Returns True/False or None if not set."""
    v = get_valkey()
    val = await v.get(f"deploy_complete:{deployment_id}")
    if val is None:
        return None
    return val == "1"


# ---- Distributed deploy lock ----

async def acquire_deploy_lock(key: str, ttl: int = 1800) -> bool:
    """Acquire a distributed lock for deploys. Returns True if acquired."""
    v = get_valkey()
    return await v.set(f"deploy_lock:{key}", "1", nx=True, ex=ttl)


async def release_deploy_lock(key: str):
    """Release a deploy lock."""
    v = get_valkey()
    await v.delete(f"deploy_lock:{key}")


async def is_deploy_locked(key: str) -> bool:
    """Check if a deploy lock is held."""
    v = get_valkey()
    return bool(await v.exists(f"deploy_lock:{key}"))
