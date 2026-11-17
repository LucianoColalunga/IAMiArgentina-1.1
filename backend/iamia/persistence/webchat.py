"""Lectura de la conversación del chat web (el navegador consulta por long polling)."""

from __future__ import annotations

from typing import Any

import asyncpg

from iamia.channels.models import Channel
from iamia.persistence.codec import DataCodec


class WebchatRepository:
    def __init__(self, pool: asyncpg.Pool, codec: DataCodec) -> None:
        self._pool = pool
        self._codec = codec

    async def messages_after(self, session_id: str, after_seq: int, *, limit: int = 50) -> list[dict[str, Any]]:
        """Respuestas de la sesión con número de secuencia mayor a `after_seq`.

        En el chat web la entrega es el propio sondeo: una respuesta se muestra apenas queda guardada,
        sin esperar a que el dispatcher la marque como enviada. Si se filtrara por "enviada", dos
        respuestas despachadas en paralelo podrían marcarse en otro orden y la de secuencia menor no
        llegaría nunca (el navegador ya pidió las posteriores a la mayor).
        """
        address_hash = self._codec.address_hash(Channel.WEBCHAT, session_id)
        rows = await self._pool.fetch(
            """
            SELECT m.id, m.seq, m.author, m.body_enc, m.created_at
              FROM messages m JOIN contacts c ON c.id = m.contact_id
             WHERE c.channel = 'webchat' AND c.address_hash = $1
               AND m.direction = 'out' AND m.status NOT IN ('failed', 'dead')
               AND m.seq > $2
             ORDER BY m.seq
             LIMIT $3
            """,
            address_hash,
            after_seq,
            min(max(limit, 1), 100),
        )
        result = []
        for row in rows:
            content = self._codec.decrypt_content(str(row["id"]), row["body_enc"])
            result.append(
                {
                    "seq": row["seq"],
                    "author": row["author"],
                    "text": content.text,
                    "sources": [{"title": s.title, "url": s.url} for s in content.sources],
                    "created_at": row["created_at"].isoformat(),
                }
            )
        return result
