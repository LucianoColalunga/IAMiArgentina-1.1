"""Qué pasa con un trabajo cuyo handler falló con una excepción no prevista.

Vuelve con espera creciente (1 s, 5 s, 30 s, 2 min, 10 min) y, agotados los intentos, va a la
cola de descarte. Reentregarlo al instante generaba, durante una caída de PostgreSQL, decenas de
intentos por segundo hasta agotar x-delivery-limit.
"""

from __future__ import annotations

import logging

from iamia.core.transient import is_transient
from iamia.messaging.contracts import Job

logger = logging.getLogger(__name__)

HANDLER_RETRY_DELAYS = (1, 5, 30, 120, 600)
HANDLER_RETRIES = len(HANDLER_RETRY_DELAYS)


def handler_retry_delay(job: Job) -> int:
    return HANDLER_RETRY_DELAYS[min(job.retries, len(HANDLER_RETRY_DELAYS) - 1)]


def log_handler_error(job: Job, exc: Exception) -> None:
    ctx = {"message_id": job.message_id, "retries": job.retries, "error": type(exc).__name__}
    if is_transient(exc):
        logger.warning("queue_handler_transient_error", extra={"ctx": ctx})  # esperable: sin traza
    else:
        logger.exception("queue_handler_error", extra={"ctx": ctx})
