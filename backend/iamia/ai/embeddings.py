"""Cliente de embeddings (API /v1/embeddings compatible con OpenAI: vLLM, TEI, llama.cpp, Ollama)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import httpx


class EmbeddingError(Exception):
    pass


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        dimensions: int | None = None,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": list(texts)},
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"servidor de embeddings no disponible: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise EmbeddingError(f"servidor de embeddings respondió {response.status_code}")
        try:
            items = sorted(response.json()["data"], key=lambda item: item.get("index", 0))
            vectors = [[float(value) for value in item["embedding"]] for item in items]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("respuesta inválida del servidor de embeddings") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError("el servidor devolvió una cantidad distinta de vectores")
        if self._dimensions and any(len(vector) != self._dimensions for vector in vectors):
            raise EmbeddingError(
                f"el modelo devolvió vectores de {len(vectors[0])} dimensiones y se esperaban {self._dimensions} "
                "(revisar EMBEDDING_DIMENSIONS)"
            )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def health(self) -> bool:
        try:
            await self.embed(["ping"])
        except EmbeddingError:
            return False
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
