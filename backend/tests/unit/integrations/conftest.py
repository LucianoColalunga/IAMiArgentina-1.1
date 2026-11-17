from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import fakeredis
import pytest

from .support import FakeApi, Reply


@pytest.fixture
async def fake_api() -> AsyncIterator[Callable[..., FakeApi]]:
    """Crea APIs falsas y cierra sus clientes al terminar la prueba."""
    apis: list[FakeApi] = []

    def factory(*replies: Reply) -> FakeApi:
        api = FakeApi(*replies)
        apis.append(api)
        return api

    yield factory
    for api in apis:
        await api.client.aclose()


@pytest.fixture
async def bytes_redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    """Redis falso sin decode_responses: los canales de Pub/Sub llegan como bytes (como en producción)."""
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()
