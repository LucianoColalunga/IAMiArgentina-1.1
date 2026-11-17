"""Pruebas contra PostgreSQL (pgvector), Redis y RabbitMQ reales.

Se ejecutan con RUN_INTEGRATION=1 y la infraestructura de tests/integration/docker-compose.yml
(o la del stack, ajustando las variables TEST_*). Cada prueba arranca con las tablas vacías.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import pytest
from redis.asyncio import Redis

from iamia.ai.engine import Answer, Outcome, Source
from iamia.channels.contracts import ChannelAdapter, ChannelCapabilities
from iamia.channels.formatting import PlainTextFormatter
from iamia.channels.models import (
    Channel,
    DeliveryError,
    Recipient,
    RenderedPart,
    SendContext,
    SendResult,
)
from iamia.channels.registry import ChannelRegistry
from iamia.conversations.context import QueryRewriter
from iamia.conversations.manager import ConversationManager, ManagerConfig
from iamia.conversations.throttle import NoContactThrottle
from iamia.core.resilience import Backoff, CircuitBreaker
from iamia.messaging.memory import MemoryJobQueue
from iamia.persistence.codec import DataCodec
from iamia.persistence.console import ConsoleRepository
from iamia.persistence.contacts import AuditRepository, ContactRepository
from iamia.persistence.database import create_pool, migrate
from iamia.persistence.inbox import InboxRepository
from iamia.persistence.maintenance import MaintenanceRepository
from iamia.persistence.webchat import WebchatRepository
from iamia.persistence.work import WorkRepository
from iamia.services.console import ConsoleService
from iamia.services.ingest import IngestService
from iamia.services.publisher import JobPublisher
from iamia.workers.inbound import InboundProcessor
from iamia.workers.maintenance import MaintenanceRunner
from iamia.workers.outbound import OutboundDispatcher

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql://iamia:iamia-test@127.0.0.1:55432/iamia")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:56379/0")
AMQP_URL = os.getenv("TEST_AMQP_URL", "amqp://iamia:iamia-test@127.0.0.1:55672/iamia")
DIMENSIONS = 64

_TABLES = "audit_log, link_codes, oauth_tokens, messages, conversations, contacts, persons, key_rotation"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="pruebas de integración: definir RUN_INTEGRATION=1")
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def migrated() -> str:
    async def run() -> None:
        pool = await create_pool(DATABASE_URL, min_size=1, max_size=2)
        try:
            await migrate(pool, embedding_dimensions=DIMENSIONS)
        finally:
            await pool.close()

    asyncio.run(run())
    return DATABASE_URL


@pytest.fixture
async def pool(migrated: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await create_pool(migrated, min_size=1, max_size=8, statement_timeout_ms=20_000)
    await pool.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    await pool.execute("DELETE FROM semantic_cache_d64")
    yield pool
    await pool.close()


@pytest.fixture
async def real_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


class FakeEngine:
    """Responde siempre lo mismo (o lo que se configure) sin modelos."""

    def __init__(self) -> None:
        self.answer_to_give = Answer(
            "Podés sacar el DNI digital desde la app Mi Argentina.",
            Outcome.ANSWERED,
            (Source("DNI digital", "https://www.argentina.gob.ar/dni"),),
        )
        self.error: Exception | None = None
        self.questions: list[str] = []
        self.chat_model: Any = None

    async def answer(self, question: str, *, client_key: str) -> Answer:
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.answer_to_give


@dataclass
class FakeSender:
    sent: list[tuple[Recipient, str, SendContext]] = field(default_factory=list)
    failures: list[DeliveryError] = field(default_factory=list)

    async def send(self, recipient: Recipient, part: RenderedPart, context: SendContext) -> SendResult:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((recipient, part.text, context))
        return SendResult(f"prov-{len(self.sent)}")


@dataclass
class Stack:
    pool: asyncpg.Pool
    codec: DataCodec
    queue: MemoryJobQueue
    inbox: InboxRepository
    work: WorkRepository
    contacts: ContactRepository
    audit: AuditRepository
    console_repo: ConsoleRepository
    maintenance_repo: MaintenanceRepository
    webchat_repo: WebchatRepository
    publisher: JobPublisher
    ingest: IngestService
    console: ConsoleService
    engine: FakeEngine
    sender: FakeSender
    inbound: InboundProcessor
    outbound: OutboundDispatcher
    maintenance: MaintenanceRunner
    breaker: CircuitBreaker

    async def drain(self) -> None:
        """Procesa la cola en memoria hasta vaciarla (entrada y salida)."""
        for _ in range(50):
            pending = list(self.queue.published)
            self.queue.published.clear()
            if not pending:
                return
            for job, _delay in pending:
                handler = self.inbound.handle if job.direction == "inbound" else self.outbound.handle
                await handler(job)


@pytest.fixture
async def stack(pool: asyncpg.Pool, real_redis: Redis, codec: DataCodec) -> Stack:
    queue = MemoryJobQueue()
    inbox = InboxRepository(pool, codec, idle_minutes=30)
    work = WorkRepository(pool, codec, context_turns=3)
    contacts = ContactRepository(pool)
    audit = AuditRepository(pool)
    publisher = JobPublisher(queue, inbox.mark_enqueued)
    engine = FakeEngine()
    sender = FakeSender()
    adapter = ChannelAdapter(
        channel=Channel.TELEGRAM,
        capabilities=ChannelCapabilities(max_text_chars=4096),
        formatter=PlainTextFormatter(4096),
        sender=sender,
    )
    registry = ChannelRegistry([adapter])
    manager = ConversationManager(
        engine=engine,  # type: ignore[arg-type]
        rewriter=QueryRewriter(None, enabled=False),  # type: ignore[arg-type]
        contacts=contacts,
        audit=audit,
        throttle=NoContactThrottle(),
        code_hasher=codec.link_code_hash,
        config=ManagerConfig(),
    )
    console_repo = ConsoleRepository(pool, codec)
    maintenance_repo = MaintenanceRepository(pool)
    breaker = CircuitBreaker("channel:telegram", 3, 30)
    no_jitter = Backoff(base=1.0, factor=2.0, max_delay=60.0, jitter=0.0)
    return Stack(
        pool=pool,
        codec=codec,
        queue=queue,
        inbox=inbox,
        work=work,
        contacts=contacts,
        audit=audit,
        console_repo=console_repo,
        maintenance_repo=maintenance_repo,
        webchat_repo=WebchatRepository(pool, codec),
        publisher=publisher,
        ingest=IngestService(inbox, publisher, real_redis),
        console=ConsoleService(console_repo, work, audit, publisher),
        engine=engine,
        sender=sender,
        inbound=InboundProcessor(
            work=work, manager=manager, registry=registry, queue=queue, publisher=publisher, backoff=no_jitter
        ),
        outbound=OutboundDispatcher(
            work=work,
            registry=registry,
            queue=queue,
            breakers={Channel.TELEGRAM: breaker},
            throttles={},
            backoff=no_jitter,
        ),
        maintenance=MaintenanceRunner(
            repository=maintenance_repo, work=work, publisher=publisher, audit=audit, semantic_cache=None
        ),
        breaker=breaker,
    )
