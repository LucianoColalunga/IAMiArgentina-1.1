"""Caché exacta de respuestas en Redis, con single-flight.

Si Redis falla, el motor sigue respondiendo sin caché (fail-open): la caché acelera, pero
nunca puede ser la razón de que el servicio se caiga.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Libera el lock solo si sigue siendo nuestro (evita borrar el lock de otro worker).
_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class AnswerCache:
    def __init__(self, redis: Redis, prefix: str = "iamia") -> None:
        self._redis = redis
        self._prefix = prefix
        self._release = redis.register_script(_RELEASE_LOCK)

    def _answer_key(self, key: str) -> str:
        return f"{self._prefix}:ans:{key}"

    def _lock_key(self, key: str) -> str:
        return f"{self._prefix}:lock:{key}"

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._redis.get(self._answer_key(key))
        except RedisError as exc:
            logger.warning("cache_unavailable", extra={"ctx": {"op": "get", "error": type(exc).__name__}})
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    async def set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        try:
            await self._redis.set(self._answer_key(key), json.dumps(payload, ensure_ascii=False), ex=ttl_seconds)
        except RedisError as exc:
            logger.warning("cache_unavailable", extra={"ctx": {"op": "set", "error": type(exc).__name__}})

    # ------------------------------------------------------------------ single-flight
    async def acquire_lock(self, key: str, ttl_seconds: int) -> str | None:
        """Si miles preguntan lo mismo a la vez, solo uno genera; el resto espera el resultado."""
        token = secrets.token_hex(8)
        try:
            acquired = await self._redis.set(self._lock_key(key), token, nx=True, ex=ttl_seconds)
        except RedisError:
            return token  # sin Redis no hay coordinación: este worker genera
        return token if acquired else None

    async def release_lock(self, key: str, token: str) -> None:
        try:
            await self._release(keys=[self._lock_key(key)], args=[token])
        except RedisError:
            pass

    async def wait_for(self, key: str, timeout_seconds: float, poll_seconds: float = 0.15) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            value = await self.get(key)
            if value is not None:
                return value
            try:
                if not await self._redis.exists(self._lock_key(key)):
                    return await self.get(key)
            except RedisError:
                return None
            await asyncio.sleep(poll_seconds)
        return None
