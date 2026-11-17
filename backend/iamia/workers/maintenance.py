"""Tareas periódicas (corre en una sola réplica; si hubiera dos, todas las tareas son idempotentes).

- Sweeper (cada 30 s): recupera mensajes con lease vencido y republica los que no llegaron a la
  cola. Es lo que garantiza que ningún mensaje guardado quede sin procesar.
- Derivaciones vencidas: si nadie tomó una conversación en el tiempo configurado, vuelve al bot
  y se avisa a la persona.
- Borrados pedidos con BORRAR, retención de datos, caché semántica vencida y versiones viejas
  de la base de conocimiento (cada hora).
- Después de rotar la clave de cifrado, recifra por lotes lo guardado con las anteriores (unos
  segundos por ciclo como mucho) hasta que no quede nada (ver persistence/rotation.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from iamia.channels.models import OutboundContent
from iamia.conversations import texts
from iamia.conversations.models import Author, OutboundDraft
from iamia.core.metrics import QUEUE_REQUEUED_BY_SWEEPER
from iamia.messaging.contracts import Job
from iamia.persistence.contacts import AuditRepository
from iamia.persistence.maintenance import MaintenanceRepository, RetentionPolicy
from iamia.persistence.rotation import KeyRotationRepository
from iamia.persistence.work import WorkRepository
from iamia.rag.store import PgVectorSemanticCache
from iamia.services.publisher import JobPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaintenanceConfig:
    sweep_interval_seconds: float = 30.0
    housekeeping_interval_seconds: float = 3600.0
    handoff_timeout_minutes: int = 240
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    kb_versions_to_keep: int = 2
    # Tiempo máximo de recifrado por ciclo: el sweeper no se demora más que esto.
    rotation_budget_seconds: float = 5.0


class MaintenanceRunner:
    def __init__(
        self,
        *,
        repository: MaintenanceRepository,
        work: WorkRepository,
        publisher: JobPublisher,
        audit: AuditRepository,
        semantic_cache: PgVectorSemanticCache | None,
        rotation: KeyRotationRepository | None = None,
        config: MaintenanceConfig | None = None,
    ) -> None:
        self._repo = repository
        self._work = work
        self._publisher = publisher
        self._audit = audit
        self._semantic = semantic_cache
        self._rotation = rotation
        self._config = config or MaintenanceConfig()
        self.last_sweep_ok = 0.0
        # Cuando todo quedó al día no se consulta más: la clave activa solo cambia con un
        # despliegue, y un proceso nuevo vuelve a mirar.
        self.rotation_done = rotation is None

    async def sweep(self) -> int:
        recovered = await self._repo.recover_expired_leases()
        pending = await self._repo.unpublished()
        jobs = [
            Job("inbound" if item.direction == "in" else "outbound", item.message_id, item.channel) for item in pending
        ]
        published = await self._publisher.publish(jobs) if jobs else 0
        for job in jobs[:published]:
            QUEUE_REQUEUED_BY_SWEEPER.labels(job.direction).inc()
        for conversation_id in await self._repo.expired_handoffs(self._config.handoff_timeout_minutes):
            created = await self._work.add_outbound(
                conversation_id, OutboundDraft(OutboundContent(texts.HANDOFF_TIMEOUT), Author.SYSTEM)
            )
            if created:
                await self._publisher.publish([Job("outbound", created[0], created[1].value)])
        if recovered or published:
            logger.info("sweep_done", extra={"ctx": {"recovered_leases": recovered, "republished": published}})
        self.last_sweep_ok = time.monotonic()
        return published

    async def housekeeping(self) -> dict[str, int]:
        result: dict[str, int] = {"erased_contacts": await self._repo.erase_requested_contacts()}
        result |= {
            f"retention_{name}": count
            for name, count in (await self._repo.apply_retention(self._config.retention)).items()
        }
        result["closed_stale"] = await self._repo.close_stale_human_conversations()
        if self._semantic is not None:
            result["semantic_cache_purged"] = await self._semantic.purge()
        result["kb_versions_dropped"] = len(await self._repo.retire_old_kb_versions(self._config.kb_versions_to_keep))
        if result["erased_contacts"]:
            await self._audit.record("system", "contacts_erased", detail={"count": result["erased_contacts"]})
        logger.info("housekeeping_done", extra={"ctx": result})
        return result

    async def rotate_keys(self) -> dict[str, int]:
        """Recifra con la clave activa, por lotes y sin pasarse del tiempo del ciclo, lo que todavía
        usa una clave anterior."""
        totals = {"rotated": 0, "failed": 0}
        if self._rotation is None or self.rotation_done:
            return totals
        deadline = time.monotonic() + self._config.rotation_budget_seconds
        pending = await self._rotation.pending()
        queue = list(pending)
        while queue and time.monotonic() < deadline:
            step = await self._rotation.step(queue[0])
            totals["rotated"] += step.rotated
            totals["failed"] += step.failed
            if step.finished:
                pending.remove(queue.pop(0))
            elif step.waiting:
                queue.pop(0)
        if totals["rotated"] or totals["failed"]:
            logger.info("key_rotation_progress", extra={"ctx": totals})
        if not pending:
            self.rotation_done = True
            logger.info("key_rotation_done", extra={"ctx": {"key_id": self._rotation.key_id}})
        return totals

    async def run(self) -> None:
        next_housekeeping = 0.0
        while True:
            try:
                await self.sweep()
                if time.monotonic() >= next_housekeeping:
                    await self.housekeeping()
                    next_housekeeping = time.monotonic() + self._config.housekeeping_interval_seconds
                await self.rotate_keys()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance_cycle_failed")
            await asyncio.sleep(self._config.sweep_interval_seconds)
