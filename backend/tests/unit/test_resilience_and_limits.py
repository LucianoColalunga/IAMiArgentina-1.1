"""Circuit breaker, backoff, límites en Redis, ritmo por canal y cola en memoria."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import asyncpg
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from iamia.ai.limits import LLMGate, TokenBudget, WindowLimiter
from iamia.conversations.throttle import RedisContactThrottle
from iamia.core.errors import ServiceBusy
from iamia.core.resilience import Backoff, CircuitBreaker, CircuitState, retry_async
from iamia.core.transient import is_transient
from iamia.messaging import memory
from iamia.messaging.contracts import Job, QueueUnavailable
from iamia.messaging.failures import HANDLER_RETRIES, handler_retry_delay
from iamia.messaging.memory import MemoryJobQueue
from iamia.messaging.rabbitmq import RETRY_TIERS, retry_tier
from iamia.messaging.throttle import ChannelThrottle
from iamia.services.publisher import JobPublisher


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class BrokenRedis:
    """Redis que no responde: los límites tienen que dejar pasar (fail-open) sin romper."""

    def pipeline(self, transaction: bool = True) -> object:
        raise RedisError("sin conexión")

    async def get(self, key: str) -> str:
        raise RedisError("sin conexión")

    async def set(self, *args: object, **kwargs: object) -> bool:
        raise RedisError("sin conexión")

    def register_script(self, script: str):
        async def run(keys: list[str], args: list[object]) -> int:
            raise RedisError("sin conexión")

        return run


# ----------------------------------------------------------------------------- circuit breaker
def test_circuit_breaker_lifecycle() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("prueba", failure_threshold=2, reset_timeout=10, clock=clock)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN and not breaker.allow()
    assert breaker.retry_after() == pytest.approx(10)

    clock.now += 10
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow()  # pasa una sola llamada de prueba
    assert not breaker.allow()
    breaker.record_failure()  # la prueba falló: se vuelve a abrir
    assert breaker.state is CircuitState.OPEN

    clock.now += 10
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED and breaker.allow()


def test_hung_probe_allows_another_after_the_timeout() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("prueba", 1, 5, clock=clock)
    breaker.record_failure()
    clock.now += 5
    assert breaker.allow() and not breaker.allow()
    clock.now += 5  # la prueba anterior quedó colgada
    assert breaker.allow()


def test_force_open_cuts_immediately() -> None:
    breaker = CircuitBreaker("prueba", 5, 30, clock=FakeClock())
    breaker.force_open()
    assert breaker.state is CircuitState.OPEN and not breaker.allow()


# ----------------------------------------------------------------------------- backoff y reintentos
def test_backoff_grows_and_is_bounded() -> None:
    backoff = Backoff(base=2, factor=3, max_delay=60, jitter=0)
    assert [backoff.delay(n) for n in range(1, 6)] == [2, 6, 18, 54, 60]
    jittered = Backoff(base=10, factor=1, max_delay=100, jitter=0.2)
    assert all(8 <= jittered.delay(1) <= 12 for _ in range(200))


async def test_retry_async() -> None:
    fast = Backoff(base=0.001, jitter=0)
    calls: list[int] = []

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("caído")
        return "ok"

    assert await retry_async(flaky, attempts=3, backoff=fast) == "ok"

    calls.clear()

    async def broken() -> str:
        calls.append(1)
        raise ConnectionError("caído")

    with pytest.raises(ConnectionError):
        await retry_async(broken, attempts=2, backoff=fast)
    assert len(calls) == 2

    calls.clear()

    async def invalid() -> str:
        calls.append(1)
        raise ValueError("dato inválido")

    with pytest.raises(ValueError):
        await retry_async(invalid, attempts=3, backoff=fast, retry_on=(ConnectionError,))
    assert len(calls) == 1  # un error que no es transitorio no se reintenta


# ----------------------------------------------------------------------------- límites en Redis
async def test_window_limiter(redis) -> None:
    limiter = WindowLimiter(redis, "prueba", 2, 60)
    now = 1_000_050.0  # 30 segundos antes de que empiece la ventana siguiente
    assert await limiter.hit("ip-1", now) == (True, 30)
    assert await limiter.hit("ip-1", now) == (True, 30)
    assert await limiter.hit("ip-1", now) == (False, 30)
    assert (await limiter.hit("ip-2", now))[0]  # otra identidad tiene su propio cupo
    assert (await limiter.hit("ip-1", now + 60))[0]  # ventana nueva
    assert await WindowLimiter(redis, "sin-limite", 0, 60).hit("ip-1") == (True, 0)


async def test_limits_fail_open_without_redis() -> None:
    broken = BrokenRedis()
    assert await WindowLimiter(broken, "prueba", 1, 60).hit("ip-1") == (True, 0)  # type: ignore[arg-type]
    budget = TokenBudget(broken, 100)  # type: ignore[arg-type]
    assert await budget.remaining("c1") == 100
    await budget.consume("c1", 10)
    assert await ChannelThrottle(broken, "x", [(1, 60)]).acquire() == 0  # type: ignore[arg-type]


async def test_token_budget(redis) -> None:
    budget = TokenBudget(redis, 100)
    assert await budget.remaining("c1") == 100
    await budget.consume("c1", 70)
    assert await budget.remaining("c1") == 30
    assert await budget.remaining("c2") == 100
    assert await TokenBudget(redis, 0).remaining("c1") > 1_000_000


async def test_llm_gate_rejects_when_saturated() -> None:
    gate = LLMGate(1, 0.05)
    async with gate.slot():
        with pytest.raises(ServiceBusy):
            async with gate.slot():
                pass
    async with gate.slot():  # el lugar se liberó
        pass


async def test_channel_throttle_sliding_windows(redis) -> None:
    throttle = ChannelThrottle(redis, "x", [(2, 900), (3, 86_400)])
    assert await throttle.acquire() == 0
    assert await throttle.acquire() == 0
    wait = await throttle.acquire()
    assert 899 < wait <= 900  # hay que esperar a que salga el envío más viejo de la ventana
    # O se registran todas las ventanas o ninguna: el intento rechazado no consumió cupo diario.
    assert await redis.zcard("iamia:{thr:x}:86400") == 2
    assert await ChannelThrottle(redis, "email", []).acquire() == 0


async def test_contact_throttle_warns_only_once(redis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("iamia.conversations.throttle.time", SimpleNamespace(time=lambda: 1_000_050.0))
    throttle = RedisContactThrottle(redis, per_minute=2, per_day=100)
    assert await throttle.check("c1") == (True, False)
    assert await throttle.check("c1") == (True, False)
    assert await throttle.check("c1") == (False, True)  # primer exceso: se avisa
    assert await throttle.check("c1") == (False, False)  # después, silencio
    assert await throttle.check("c2") == (True, False)


# ----------------------------------------------------------------------------- trabajos y cola
def test_job_roundtrip_and_routing() -> None:
    job = Job("outbound", "m-1", "whatsapp", "t-1")
    assert job.routing_key == "outbound.whatsapp"
    assert Job("inbound", "m-2", "sms").routing_key == "inbound"
    assert Job.from_bytes(job.retried().to_bytes()) == Job("outbound", "m-1", "whatsapp", "t-1", retries=1)
    # El trabajo solo lleva identificadores: nada de texto ni datos personales.
    assert set(json.loads(job.to_bytes())) == {"d", "id", "ch", "t", "r"}
    with pytest.raises(ValueError):
        Job.from_bytes(b'{"d":"otro","id":"x","ch":"sms"}')


@pytest.mark.parametrize(("delay", "tier"), [(0, 1), (1, 1), (3, 5), (31, 120), (5000, 1800)])
def test_retry_tiers(delay: float, tier: int) -> None:
    assert retry_tier(delay) == tier and tier in RETRY_TIERS


async def test_memory_queue_backpressure_and_outage() -> None:
    queue = MemoryJobQueue(max_length=1)
    await queue.publish(Job("inbound", "m-1", "sms"))
    with pytest.raises(QueueUnavailable):
        await queue.publish(Job("inbound", "m-2", "sms"))
    assert await queue.depth("inbound") == 1
    queue.fail_publish = True
    with pytest.raises(QueueUnavailable):
        await queue.publish(Job("inbound", "m-3", "sms"))
    assert not await queue.ping()


@pytest.mark.parametrize(("retries", "delay"), [(0, 1), (1, 5), (2, 30), (3, 120), (4, 600), (9, 600)])
def test_failed_jobs_wait_longer_each_time(retries: int, delay: int) -> None:
    assert handler_retry_delay(Job("inbound", "m", "sms", retries=retries)) == delay


def test_transient_errors_are_told_apart_from_bugs() -> None:
    assert is_transient(asyncpg.CannotConnectNowError("the database system is shutting down"))
    assert is_transient(asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"))
    assert is_transient(ConnectionResetError(104, "Connection reset by peer"))
    assert is_transient(RedisConnectionError("Error 111 connecting to redis:6379"))
    assert not is_transient(KeyError("x"))
    assert not is_transient(asyncpg.UniqueViolationError("duplicate key"))


async def test_failed_jobs_come_back_later_and_finally_go_to_the_dlq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "handler_retry_delay", lambda job: 0.01)
    queue = MemoryJobQueue()
    seen: list[int] = []

    async def always_fails(job: Job) -> None:
        seen.append(job.retries)
        raise RuntimeError("falla")

    consumer = asyncio.create_task(queue.consume("inbound", always_fails, prefetch=1))
    await queue.publish(Job("inbound", "m-1", "sms"))
    for _ in range(200):
        if queue.dead:
            break
        await asyncio.sleep(0.01)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    assert seen == list(range(HANDLER_RETRIES + 1))  # el intento original y uno por cada espera
    assert queue.dead == [(Job("inbound", "m-1", "sms", retries=HANDLER_RETRIES), "handler:RuntimeError")]


async def test_publisher_leaves_unconfirmed_jobs_to_the_sweeper() -> None:
    marked: list[str] = []

    async def mark(ids: list[str]) -> None:
        marked.extend(ids)

    queue = MemoryJobQueue()
    publisher = JobPublisher(queue, mark)
    assert await publisher.publish([Job("inbound", "a", "sms"), Job("inbound", "b", "sms")]) == 2
    assert marked == ["a", "b"]
    queue.fail_publish = True
    assert await publisher.publish([Job("inbound", "c", "sms")]) == 0  # no lanza: lo republica el sweeper
    assert marked == ["a", "b"]
