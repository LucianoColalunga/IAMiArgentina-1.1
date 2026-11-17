"""Proceso de un worker: servidor de salud y métricas, señales y apagado ordenado.

Kubernetes usa /healthz (liveness: el proceso responde) y /readyz (readiness: sus dependencias
responden). Prometheus lee /metrics. El servidor es mínimo (asyncio puro) para no sumar
dependencias a los workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Sequence

from iamia.core.metrics import render_metrics

logger = logging.getLogger(__name__)

ReadinessCheck = Callable[[], Awaitable[bool]]


class HealthServer:
    def __init__(self, port: int, readiness: Sequence[ReadinessCheck] = ()) -> None:
        self._port = port
        self._readiness = list(readiness)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="0.0.0.0", port=self._port)  # noqa: S104

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _ready(self) -> bool:
        for check in self._readiness:
            try:
                if not await asyncio.wait_for(check(), timeout=3):
                    return False
            except Exception:
                return False
        return True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            while (line := await asyncio.wait_for(reader.readline(), timeout=5)) not in (b"\r\n", b"\n", b""):
                del line
            parts = request_line.decode("latin-1").split()
            path = parts[1].split("?", 1)[0] if len(parts) >= 2 else "/"
            if path == "/healthz":
                status, body, media = 200, b"ok", "text/plain"
            elif path == "/readyz":
                ready = await self._ready()
                status, body, media = (200, b"ready", "text/plain") if ready else (503, b"not ready", "text/plain")
            elif path == "/metrics":
                body, media = render_metrics()
                status = 200
            else:
                status, body, media = 404, b"not found", "text/plain"
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: {media}\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def run_until_stopped(tasks: Sequence[Awaitable[None]], *, shutdown_grace_seconds: float = 30.0) -> None:
    """Corre las tareas hasta SIGTERM/SIGINT y las cancela ordenadamente."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    running = [asyncio.ensure_future(task) for task in tasks]
    stopper = asyncio.ensure_future(stop.wait())
    done, _ = await asyncio.wait([*running, stopper], return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if task is not stopper and not task.cancelled() and task.exception() is not None:
            logger.error("worker_task_crashed", exc_info=task.exception())
    logger.info("worker_stopping")
    for task in running:
        task.cancel()
    await asyncio.wait(running, timeout=shutdown_grace_seconds)
    stopper.cancel()
