"""Ritmo de envío por canal, compartido entre todas las réplicas del dispatcher (Redis).

Ventana deslizante exacta (ZSET por ventana) evaluada en un script Lua atómico: o se registran
todas las ventanas o ninguna, y se devuelve cuánto esperar si alguna está llena. Las claves
usan hash tag ({thr:canal}) para caer en el mismo slot si Redis corre en modo cluster.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Sequence

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_SCRIPT = """
local now = tonumber(ARGV[1])
local wait = 0
for i, key in ipairs(KEYS) do
  local limit = tonumber(ARGV[1 + 2 * i])
  local window = tonumber(ARGV[2 + 2 * i])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  if redis.call('ZCARD', key) >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local remaining = tonumber(oldest[2]) + window - now
    if remaining > wait then wait = remaining end
  end
end
if wait > 0 then return wait end
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[2 + 2 * i])
  redis.call('ZADD', key, now, ARGV[2])
  redis.call('PEXPIRE', key, window + 1000)
end
return 0
"""


class ChannelThrottle:
    def __init__(self, redis: Redis, channel: str, windows: Sequence[tuple[int, int]]) -> None:
        """windows: [(cantidad, segundos), ...], por ejemplo [(15, 900), (1440, 86400)] para X."""
        self._redis = redis
        self._channel = channel
        self._windows = [(int(limit), int(seconds)) for limit, seconds in windows if limit > 0 and seconds > 0]
        self._keys = [f"iamia:{{thr:{channel}}}:{seconds}" for _, seconds in self._windows]
        self._script = redis.register_script(_SCRIPT) if self._windows else None

    async def acquire(self) -> float:
        """Reserva un envío. Devuelve 0 si se puede enviar ya, o los segundos a esperar."""
        if self._script is None:
            return 0.0
        now_ms = int(time.time() * 1000)
        args: list[int | str] = [now_ms, f"{now_ms}-{secrets.token_hex(4)}"]
        for limit, seconds in self._windows:
            args.extend([limit, seconds * 1000])
        try:
            wait_ms = await self._script(keys=self._keys, args=args)
        except RedisError as exc:
            # Sin Redis no se frena: los límites del proveedor (429) siguen protegiendo y se reintenta.
            logger.warning(
                "throttle_unavailable", extra={"ctx": {"channel": self._channel, "error": type(exc).__name__}}
            )
            return 0.0
        return max(0.0, float(wait_ms) / 1000)
