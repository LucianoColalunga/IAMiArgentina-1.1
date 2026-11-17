"""Cliente del modelo de lenguaje local.

Habla la API de chat compatible con OpenAI, que exponen vLLM (producción en GPU), llama.cpp
(CPU), Ollama y TGI. Así el modelo y el servidor de inferencia se pueden cambiar sin tocar el
resto de la plataforma: alcanza con cambiar LLM_BASE_URL y LLM_MODEL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None


class LLMError(Exception):
    """Error del servidor de inferencia. `retryable` indica si tiene sentido reintentar."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMTimeout(LLMError):
    pass


class ChatModel(Protocol):
    @property
    def model_name(self) -> str: ...

    async def complete(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None, temperature: float | None = None
    ) -> Completion: ...

    async def health(self) -> bool: ...


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # algunos servidores devuelven bloques de contenido
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


class OpenAICompatibleChatModel:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.1,
        max_tokens: int = 512,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None, temperature: float | None = None
    ) -> Completion:
        payload = {
            "model": self._model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._max_tokens,
            "stream": False,
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeout("el modelo no respondió a tiempo") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"no se pudo conectar con el servidor de inferencia: {type(exc).__name__}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise LLMError(f"servidor de inferencia respondió {response.status_code}")
        if response.status_code >= 400:
            raise LLMError(f"pedido rechazado por el servidor de inferencia ({response.status_code})", retryable=False)
        try:
            data = response.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("respuesta inválida del servidor de inferencia") from exc
        usage = data.get("usage") or {}
        return Completion(
            text=_message_text((choice.get("message") or {}).get("content")),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
        )

    async def health(self) -> bool:
        try:
            response = await self._client.get(f"{self._base_url}/models", headers=self._headers, timeout=3.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
