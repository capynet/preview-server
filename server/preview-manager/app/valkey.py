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


async def deploy_log_exists(deployment_id: int) -> bool:
    """Check if this deployment is tracked as active in Valkey."""
    v = get_valkey()
    return await v.sismember("active_deployments", str(deployment_id))


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


# ---- Deploy cancellation ----

async def request_deploy_cancel(key: str):
    """Request cancellation of the current deploy for a preview."""
    v = get_valkey()
    await v.set(f"deploy_cancel:{key}", "1", ex=300)  # TTL 5min


async def is_deploy_cancelled(key: str) -> bool:
    """Check if cancellation was requested for this deploy."""
    v = get_valkey()
    return bool(await v.exists(f"deploy_cancel:{key}"))


async def clear_deploy_cancel(key: str):
    """Clear the cancellation flag."""
    v = get_valkey()
    await v.delete(f"deploy_cancel:{key}")


# ---- Single-writer compute lock (shared across uvicorn workers) ----

async def acquire_compute_lock(name: str, ttl: int) -> bool:
    """Elect a single writer per ttl window.

    Returns True for exactly one worker; the lock auto-expires after ttl so the
    next cycle re-elects (and a dead writer is transparently replaced). Used to
    stop every uvicorn worker from running the same periodic job independently.
    """
    v = get_valkey()
    return bool(await v.set(f"compute_lock:{name}", "1", nx=True, ex=ttl))


# ---- Disk usage snapshot (shared across uvicorn workers) ----

async def set_disk_usage(data: dict):
    """Store the disk-usage snapshot so every worker can serve it."""
    v = get_valkey()
    await v.set("disk_usage:cache", json.dumps(data))


async def get_disk_usage() -> Optional[dict]:
    """Read the shared disk-usage snapshot. None if not computed yet."""
    v = get_valkey()
    val = await v.get("disk_usage:cache")
    if val is None:
        return None
    try:
        return json.loads(val)
    except (ValueError, TypeError):
        return None


# ---- Maintenance mode (control-plane drain) ----

MAINTENANCE_KEY = "maintenance:state"


async def set_maintenance(active: bool, reason: str = "", by: str = "", level: str = "drain"):
    """Set (or clear) the global maintenance flag.

    Stored as a hash so API, worker and UI share a single source of truth.
    level: "drain" (UI read-only + banner, deploys parked) or "full" (UI blocked).
    """
    v = get_valkey()
    if not active:
        await v.delete(MAINTENANCE_KEY)
        return
    from datetime import datetime, timezone
    await v.hset(MAINTENANCE_KEY, mapping={
        "active": "1",
        "reason": reason or "",
        "by": by or "",
        "level": level if level in ("drain", "full") else "drain",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })


async def get_maintenance() -> dict:
    """Return the current maintenance state. Always safe to call.

    Returns {"active": False} when the flag is unset or Valkey is unavailable,
    so callers never crash on a maintenance check.
    """
    try:
        v = get_valkey()
        data = await v.hgetall(MAINTENANCE_KEY)
    except Exception:
        return {"active": False}
    if not data or data.get("active") != "1":
        return {"active": False}
    return {
        "active": True,
        "reason": data.get("reason", ""),
        "by": data.get("by", ""),
        "level": data.get("level", "drain"),
        "started_at": data.get("started_at", ""),
    }


async def is_maintenance_active() -> bool:
    """Fast boolean check for the maintenance flag (fail-open to False)."""
    try:
        v = get_valkey()
        return await v.hget(MAINTENANCE_KEY, "active") == "1"
    except Exception:
        return False


async def list_active_deploy_locks() -> list[str]:
    """Return the deploy keys that currently hold a lock (deploys in flight).

    Uses SCAN (never KEYS) so it is safe on a live Valkey. Used by the drain
    step to know when it is safe to restart the worker.
    """
    v = get_valkey()
    keys: list[str] = []
    async for key in v.scan_iter(match="deploy_lock:*", count=100):
        keys.append(key.removeprefix("deploy_lock:"))
    return keys
