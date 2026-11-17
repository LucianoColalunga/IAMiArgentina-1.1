"""Límite de mensajes por contacto (por minuto y por día).

Protege el costo del modelo y corta bucles con otros bots que responden automáticamente. Al
excederse se avisa una sola vez por ventana; los mensajes siguientes se ignoran en silencio.
"""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from iamia.ai.limits import WindowLimiter

logger = logging.getLogger(__name__)


class RedisContactThrottle:
    def __init__(self, redis: Redis, *, per_minute: int, per_day: int) -> None:
        self._redis = redis
        self._minute = WindowLimiter(redis, "contact_min", per_minute, 60)
        self._day = WindowLimiter(redis, "contact_day", per_day, 86_400)

    async def check(self, contact_key: str) -> tuple[bool, bool]:
        now = time.time()
        for limiter, window in ((self._minute, 60), (self._day, 86_400)):
            allowed, _ = await limiter.hit(contact_key, now)
            if not allowed:
                return False, await self._first_notice(contact_key, window, now)
        return True, False

    async def _first_notice(self, contact_key: str, window: int, now: float) -> bool:
        key = f"iamia:rlnotice:{contact_key}:{window}:{int(now // window)}"
        try:
            return bool(await self._redis.set(key, "1", nx=True, ex=window + 5))
        except RedisError as exc:
            logger.warning("throttle_notice_unavailable", extra={"ctx": {"error": type(exc).__name__}})
            return False


class NoContactThrottle:
    async def check(self, contact_key: str) -> tuple[bool, bool]:
        return True, False
