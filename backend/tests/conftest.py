"""Fixtures comunes a todas las pruebas."""

from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis
import pytest

from iamia.persistence.codec import DataCodec
from iamia.security.crypto import FieldCipher, Pseudonymizer, generate_key


@pytest.fixture
def codec() -> DataCodec:
    return DataCodec(FieldCipher.from_config(f"k1:{generate_key()}"), Pseudonymizer.from_config(generate_key()))


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()
