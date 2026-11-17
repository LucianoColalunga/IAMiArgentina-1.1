"""Contrato de la cola de trabajos.

Los trabajos solo llevan ids (nunca texto ni datos personales): el contenido vive cifrado en
PostgreSQL y el worker lo lee al procesar. Así la cola no guarda información sensible en disco.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

Direction = Literal["inbound", "outbound"]


class QueueUnavailable(Exception):
    """No se pudo publicar (broker caído, cola llena o sin confirmación)."""


@dataclass(frozen=True, slots=True)
class Job:
    direction: Direction
    message_id: str
    channel: str
    trace_id: str = ""
    retries: int = 0

    @property
    def routing_key(self) -> str:
        return "inbound" if self.direction == "inbound" else f"outbound.{self.channel}"

    def retried(self) -> Job:
        return replace(self, retries=self.retries + 1)

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"d": self.direction, "id": self.message_id, "ch": self.channel, "t": self.trace_id, "r": self.retries},
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> Job:
        data = json.loads(raw)
        direction = data["d"]
        if direction not in ("inbound", "outbound"):
            raise ValueError("dirección de trabajo inválida")
        return cls(direction, str(data["id"]), str(data["ch"]), str(data.get("t", "")), int(data.get("r", 0)))


JobHandler = Callable[[Job], Awaitable[None]]


class JobQueue(Protocol):
    async def publish(self, job: Job, *, delay_seconds: float = 0.0) -> None:
        """Publica con confirmación del broker. Lanza QueueUnavailable si no se confirma."""
        ...

    async def dead_letter(self, job: Job, reason: str) -> None: ...

    async def consume(self, routing_key: str, handler: JobHandler, *, prefetch: int) -> None:
        """Consume hasta que se cancele la tarea. El trabajo se confirma después del handler."""
        ...

    async def depth(self, routing_key: str) -> int | None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...
