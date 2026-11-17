"""Servidor de IA simulado con API compatible con OpenAI (/v1/chat/completions y /v1/embeddings).

Sirve para las pruebas de punta a punta: responde en milisegundos, siempre igual, y permite
inyectar demoras y errores. No es un modelo: arma la respuesta con la primera oración del
contexto oficial que recibió y cita su fuente, que es lo que se necesita para verificar el
circuito completo (recuperación, prompt, guard, caché y envío por cada canal).

    python -m uvicorn mock_ai:app --app-dir tests/e2e/simulators --port 9001
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DIMENSIONS = int(os.getenv("MOCK_EMBEDDING_DIMENSIONS", "256"))
_WORD = re.compile(r"[a-z0-9]+")
_CONTEXT = re.compile(r"<contexto_oficial>(.*?)</contexto_oficial>", re.DOTALL)
_QUESTION = re.compile(r"<pregunta_usuario>(.*?)</pregunta_usuario>", re.DOTALL)
_HISTORY = re.compile(r"<historial>(.*?)</historial>", re.DOTALL)
_CITIZEN = re.compile(r"^- Ciudadano: (.+)$", re.MULTILINE)
_ANSWER = re.compile(r"Respuesta:\s*(.+)")
_URL = re.compile(r"https://[^\s<>\"')]+")

app = FastAPI(title="IA simulada")


class Faults:
    """Fallas programadas desde las pruebas (POST /_control)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.chat_failures = 0  # próximas N generaciones responden 500
        self.embedding_failures = 0
        self.chat_delay = 0.0
        self.calls = {"chat": 0, "embeddings": 0, "rewrite": 0}
        self.prompts: list[str] = []


faults = Faults()


# Palabras sin contenido: no ayudan a distinguir un trámite de otro.
_STOPWORDS = frozenset(
    """
    el la los las un una unos unas de del al a en y o u que se mi mis tu tus su sus me te lo le les
    es son ser por para con sin como cual cuales quien quienes donde cuando cuanto cuanta hay hago
    puedo puede tengo tiene necesito sobre este esta esto eso ese esa ya si no mas muy hola
    """.split()
)
_ACCENTS = str.maketrans("áéíóúüñ", "aeiouun")
_HEADER = re.compile(r"^(Trámite|Pregunta):\s*(.*)$", re.MULTILINE)


def _terms(text: str) -> list[str]:
    words = _WORD.findall(text.lower().translate(_ACCENTS))
    return [word[:5] for word in words if word not in _STOPWORDS and len(word) >= 3]


def _vector(text: str) -> list[float]:
    """Bolsa de términos con hashing, normalizada (similitud coseno).

    Un fragmento de la base (líneas "Trámite:", "Pregunta:" y "Respuesta:") se representa por su
    trámite y su pregunta: así una consulta parecida a la pregunta queda cerca, como pasaría con
    un modelo de embeddings real, y una consulta de otro tema queda lejos.
    """
    headers = _HEADER.findall(text)
    source = " ".join(value for _, value in headers) if headers else text
    values = [0.0] * DIMENSIONS
    for term in _terms(source):
        bucket = int(hashlib.sha256(term.encode()).hexdigest(), 16) % DIMENSIONS
        values[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _answer_from_context(user_message: str) -> str:
    context_match = _CONTEXT.search(user_message)
    context = context_match.group(1) if context_match else ""
    answer = _ANSWER.search(context)
    url = _URL.search(context)
    if not answer:
        return "No encontré información oficial sobre esa consulta."
    first_sentence = answer.group(1).strip().split(". ")[0].rstrip(".")
    source = f" Fuente: {url.group(0)}" if url else ""
    return f"Según la información oficial: {first_sentence}.{source}"


def _rewrite(user_message: str) -> str:
    """Como haría un modelo: la consulta de seguimiento se completa con el tema de la anterior."""
    question = _QUESTION.search(user_message)
    text = (question.group(1) if question else user_message).strip()
    history = _HISTORY.search(user_message)
    previous = _CITIZEN.findall(history.group(1)) if history else []
    if not previous:
        return text
    return f"{text.rstrip('?')} (sobre: {previous[-1].strip()})"


def _usage(prompt: str, completion: str) -> dict[str, int]:
    return {
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(completion) // 4),
        "total_tokens": max(2, (len(prompt) + len(completion)) // 4),
    }


@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    messages: list[dict[str, Any]] = body.get("messages") or []
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    is_rewrite = "reescribir" in system.lower()
    faults.calls["rewrite" if is_rewrite else "chat"] += 1
    faults.prompts.append(user)
    if faults.chat_delay:
        await asyncio.sleep(faults.chat_delay)
    if faults.chat_failures > 0 and not is_rewrite:
        faults.chat_failures -= 1
        return JSONResponse({"error": {"message": "falla simulada"}}, status_code=500)
    if is_rewrite:
        text = _rewrite(user)
    else:
        text = _answer_from_context(user)
    return JSONResponse(
        {
            "id": f"chatcmpl-{time.time_ns()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": _usage(user, text),
        }
    )


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    body = await request.json()
    inputs = body.get("input")
    texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
    faults.calls["embeddings"] += 1
    if faults.embedding_failures > 0:
        faults.embedding_failures -= 1
        return JSONResponse({"error": {"message": "falla simulada"}}, status_code=500)
    data = [{"object": "embedding", "index": i, "embedding": _vector(text)} for i, text in enumerate(texts)]
    tokens = sum(max(1, len(text) // 4) for text in texts)
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": body.get("model", "mock"),
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }
    )


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "mock-chat", "object": "model"}, {"id": "mock-embeddings", "object": "model"}],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_control")
async def control(request: Request) -> dict[str, Any]:
    body = await request.json()
    if body.get("reset"):
        faults.reset()
    faults.chat_failures = int(body.get("chat_failures", faults.chat_failures))
    faults.embedding_failures = int(body.get("embedding_failures", faults.embedding_failures))
    faults.chat_delay = float(body.get("chat_delay", faults.chat_delay))
    return {"ok": True}


@app.get("/_stats")
async def stats() -> dict[str, Any]:
    return {"calls": faults.calls, "prompts": faults.prompts[-50:]}
