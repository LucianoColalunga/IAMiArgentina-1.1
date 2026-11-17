"""Límites de uso: ventanas fijas en Redis, presupuesto diario de tokens y compuerta del LLM."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

from iamia.core.errors import ServiceBusy

logger = logging.getLogger(__name__)


class WindowLimiter:
    """Límite de eventos por ventana fija (compartido entre réplicas vía Redis).

    Si Redis no responde se deja pasar (fail-open) y se registra: es preferible atender sin
    límite unos segundos que rechazar a todos.
    """

    def __init__(self, redis: Redis, name: str, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._name = name
        self._limit = limit
        self._window = window_seconds

    async def hit(self, identity: str, now: float | None = None) -> tuple[bool, int]:
        """Registra un evento. Devuelve (permitido, segundos hasta la próxima ventana)."""
        if self._limit <= 0:
            return True, 0
        current = time.time() if now is None else now
        window = int(current // self._window)
        key = f"iamia:rl:{self._name}:{identity}:{window}"
        retry_after = max(1, int((window + 1) * self._window - current))
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window + 5)
                count, _ = await pipe.execute()
        except RedisError as exc:
            logger.warning(
                "rate_limit_unavailable", extra={"ctx": {"limiter": self._name, "error": type(exc).__name__}}
            )
            return True, 0
        return int(count) <= self._limit, retry_after


class TokenBudget:
    """Presupuesto diario de tokens del LLM por cliente (evita que alguien agote la GPU)."""

    def __init__(self, redis: Redis, daily_limit: int) -> None:
        self._redis = redis
        self._limit = daily_limit

    def _key(self, client: str) -> str:
        return f"iamia:tok:{client}:{datetime.now(UTC):%Y%m%d}"

    async def remaining(self, client: str) -> int:
        if self._limit <= 0:
            return 1 << 30
        try:
            used = int(await self._redis.get(self._key(client)) or 0)
        except RedisError:
            return self._limit
        return self._limit - used

    async def consume(self, client: str, tokens: int) -> None:
        if self._limit <= 0 or tokens <= 0:
            return
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incrby(self._key(client), tokens)
                pipe.expire(self._key(client), 26 * 3600)
                await pipe.execute()
        except RedisError:
            pass


class LLMGate:
    """Limita las generaciones simultáneas por proceso (backpressure hacia el servidor de inferencia).

    Si no se consigue un lugar en `queue_timeout` segundos se responde "ocupado" en vez de
    acumular pedidos que terminarían todos por timeout.
    """

    def __init__(self, max_concurrent: int, queue_timeout: float) -> None:
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._queue_timeout = queue_timeout

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError as exc:
            raise ServiceBusy("el modelo está saturado", retry_after=5) from exc
        try:
            yield
        finally:
            self._semaphore.release()
