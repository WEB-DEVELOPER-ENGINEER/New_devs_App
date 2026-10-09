import json
import logging
import os
import asyncio
from typing import Dict, Any, Optional
import redis.asyncio as redis
from app.services.reservations import (
    calculate_total_revenue,
    get_property,
    PropertyNotFoundError
)

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis_client = None
_redis_loop = None


def get_redis_client() -> redis.Redis:
    """Get or create loop-aware Redis client."""
    global _redis_client, _redis_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _redis_client is None or _redis_loop != current_loop:
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        _redis_loop = current_loop
    return _redis_client


class RedisProxy:
    """Proxy object so existing imports of redis_client remain compatible."""
    def __getattr__(self, name):
        client = get_redis_client()
        return getattr(client, name)


redis_client = RedisProxy()


def build_cache_key(
    tenant_id: str,
    property_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None
) -> str:
    """
    Constructs a strictly tenant-isolated cache key.
    Format: revenue:{tenant_id}:{property_id}[:{year}:{month}]
    """
    if month is not None and year is not None:
        return f"revenue:{tenant_id}:{property_id}:{year}:{month}"
    return f"revenue:{tenant_id}:{property_id}"


async def get_revenue_summary(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None
) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing tenant-scoped caching to improve performance
    without cross-tenant leakage.
    Validates property ownership first to ensure cache hits cannot bypass authorization.
    """
    # 0. Always validate property ownership before retrieving cached data or computing
    prop = await get_property(property_id, tenant_id)
    if not prop:
        raise PropertyNotFoundError(f"Property '{property_id}' not found for tenant '{tenant_id}'")

    cache_key = build_cache_key(tenant_id, property_id, month, year)
    client = get_redis_client()

    # 1. Try to read from cache (fail-open if Redis error)
    try:
        cached = await client.get(cache_key)
        if cached:
            parsed = json.loads(cached)
            # Extra safety: verify cached payload matches requesting tenant
            if parsed.get("tenant_id") == tenant_id and parsed.get("property_id") == property_id:
                logger.debug(f"Cache hit for key {cache_key}")
                return parsed
            else:
                logger.warning(
                    f"Cache poisoning detected or tenant mismatch for key {cache_key}. Evicting."
                )
                await client.delete(cache_key)
    except Exception as e:
        logger.warning(f"Redis get failed for {cache_key}: {e}. Falling back to database calculation.")

    # 2. Calculate revenue from database
    result = await calculate_total_revenue(
        property_id=property_id,
        tenant_id=tenant_id,
        month=month,
        year=year
    )

    # 3. Cache the result for 5 minutes (fail-safe if Redis error)
    try:
        await client.set(cache_key, json.dumps(result), ex=300)
    except Exception as e:
        logger.warning(f"Redis set failed for {cache_key}: {e}")

    return result


async def invalidate_revenue_cache(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None
) -> None:
    """Invalidate cached revenue for a specific property and tenant."""
    cache_key = build_cache_key(tenant_id, property_id, month, year)
    client = get_redis_client()
    try:
        await client.delete(cache_key)
        logger.info(f"Invalidated cache key: {cache_key}")
    except Exception as e:
        logger.warning(f"Failed to invalidate cache key {cache_key}: {e}")
