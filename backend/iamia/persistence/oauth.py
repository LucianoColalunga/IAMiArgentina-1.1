"""Almacén de tokens OAuth en PostgreSQL (cifrados) con lock entre procesos."""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import asyncpg

from iamia.integrations.x.tokens import StoredToken
from iamia.persistence.codec import DataCodec


class PgTokenStore:
    def __init__(self, pool: asyncpg.Pool, codec: DataCodec) -> None:
        self._pool = pool
        self._codec = codec

    async def load(self, provider: str) -> StoredToken | None:
        row = await self._pool.fetchrow(
            "SELECT access_token_enc, refresh_token_enc, expires_at FROM oauth_tokens WHERE provider = $1", provider
        )
        if row is None:
            return None
        return StoredToken(
            access_token=self._codec.decrypt_secret(f"{provider}:access", row["access_token_enc"]),
            refresh_token=self._codec.decrypt_secret(f"{provider}:refresh", row["refresh_token_enc"]),
            expires_at=row["expires_at"],
        )

    async def save(self, provider: str, token: StoredToken) -> None:
        await self._pool.execute(
            """
            INSERT INTO oauth_tokens (provider, access_token_enc, refresh_token_enc, expires_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (provider) DO UPDATE
               SET access_token_enc = EXCLUDED.access_token_enc, refresh_token_enc = EXCLUDED.refresh_token_enc,
                   expires_at = EXCLUDED.expires_at, updated_at = now()
            """,
            provider,
            self._codec.encrypt_secret(f"{provider}:access", token.access_token),
            self._codec.encrypt_secret(f"{provider}:refresh", token.refresh_token),
            token.expires_at,
        )

    def lock(self, provider: str) -> AbstractAsyncContextManager[None]:
        return self._advisory_lock(provider)

    @asynccontextmanager
    async def _advisory_lock(self, provider: str) -> AsyncIterator[None]:
        key = zlib.crc32(f"oauth:{provider}".encode())
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", key)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", key)
