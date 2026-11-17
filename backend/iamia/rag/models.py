"""Modelos y contratos de la recuperación de información."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class KBInfo:
    """Versión activa de la base de conocimiento."""

    version: str
    table: str
    embed_model: str
    dimensions: int


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    text: str
    title: str
    url: str
    score: float  # similitud coseno (1 = idéntico)


class Retriever(Protocol):
    async def active(self) -> KBInfo | None: ...

    async def search(self, kb: KBInfo, embedding: Sequence[float], top_k: int) -> list[RetrievedChunk]: ...

    async def ping(self) -> bool: ...


class SemanticCache(Protocol):
    async def lookup(self, embedding: Sequence[float], scope: str) -> str | None: ...

    async def store(self, embedding: Sequence[float], scope: str, answer_key: str) -> None: ...
