"""Patrones de resiliencia: circuit breaker y backoff exponencial con jitter."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum

from iamia.core.metrics import CIRCUIT_STATE

logger = logging.getLogger(__name__)


class CircuitState(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitBreaker:
    """Deja de llamar a una dependencia que viene fallando y la vuelve a probar de a una llamada.

    - CLOSED: todo pasa; se cuentan fallas consecutivas.
    - OPEN: nada pasa durante `reset_timeout` segundos (se responde rápido sin saturar al otro).
    - HALF_OPEN: pasa una sola llamada de prueba; si sale bien se cierra, si falla se reabre.

    Es por proceso: cada worker aprende solo. Es intencional, evita depender de un estado
    compartido justo cuando las dependencias están fallando.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = max(1, failure_threshold)
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_started_at: float | None = None
        CIRCUIT_STATE.labels(name).set(CircuitState.CLOSED)

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self._reset_timeout:
            return CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        now = self._clock()
        if self._state is CircuitState.OPEN:
            if now - self._opened_at < self._reset_timeout:
                return False
            self._set(CircuitState.HALF_OPEN)
        if self._state is CircuitState.HALF_OPEN:
            # Una sola prueba a la vez; si la prueba quedó colgada, se habilita otra.
            if self._probe_started_at is not None and now - self._probe_started_at < self._reset_timeout:
                return False
            self._probe_started_at = now
        return True

    def retry_after(self) -> float:
        """Segundos hasta que el circuito vuelva a dejar pasar una prueba."""
        if self._state is not CircuitState.OPEN:
            return 0.0
        return max(0.0, self._reset_timeout - (self._clock() - self._opened_at))

    def record_success(self) -> None:
        self._failures = 0
        self._probe_started_at = None
        if self._state is not CircuitState.CLOSED:
            logger.info("circuit_closed", extra={"ctx": {"circuit": self.name}})
            self._set(CircuitState.CLOSED)

    def record_failure(self) -> None:
        self._probe_started_at = None
        if self._state is CircuitState.HALF_OPEN:
            self._open()
            return
        self._failures += 1
        if self._state is CircuitState.CLOSED and self._failures >= self._threshold:
            self._open()

    def force_open(self) -> None:
        """Abre el circuito ya: la falla afecta a todo el servicio (credenciales vencidas, cuenta bloqueada)."""
        self._probe_started_at = None
        self._failures = max(self._failures, self._threshold)
        self._open()

    def _open(self) -> None:
        self._opened_at = self._clock()
        if self._state is not CircuitState.OPEN:
            logger.warning(
                "circuit_opened",
                extra={"ctx": {"circuit": self.name, "failures": self._failures, "reset_s": self._reset_timeout}},
            )
        self._set(CircuitState.OPEN)

    def _set(self, state: CircuitState) -> None:
        self._state = state
        CIRCUIT_STATE.labels(self.name).set(int(state))


@dataclass(frozen=True)
class Backoff:
    """Demora exponencial con jitter: base * factor^(intento-1), acotada y con ±jitter."""

    base: float = 2.0
    factor: float = 3.0
    max_delay: float = 600.0
    jitter: float = 0.2

    def delay(self, attempt: int) -> float:
        raw = min(self.max_delay, self.base * (self.factor ** max(0, attempt - 1)))
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))  # noqa: S311 - jitter, no criptografía


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff: Backoff,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    description: str = "operación",
) -> T:
    """Reintenta una operación asincrónica con backoff. Relanza la última excepción."""
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            if attempt >= attempts:
                raise
            wait = backoff.delay(attempt)
            logger.warning(
                "retrying",
                extra={
                    "ctx": {"operation": description, "attempt": attempt, "wait_s": round(wait, 2), "error": repr(exc)}
                },
            )
            await asyncio.sleep(wait)
    raise RuntimeError("inalcanzable")
