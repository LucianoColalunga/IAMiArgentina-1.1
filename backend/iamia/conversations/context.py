"""Contexto conversacional.

El motor RAG responde preguntas independientes (así sus respuestas se pueden cachear y
compartir entre personas). Cuando un mensaje depende de lo anterior ("¿y cuánto sale?"), se
reescribe como pregunta completa usando los últimos turnos de la conversación y recién después
pasa por el pipeline normal. Si la reescritura falla o parece manipulada, se usa el mensaje tal
cual: el contexto mejora la respuesta pero nunca la bloquea.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence

from iamia.ai.guard import guard_rewrite
from iamia.ai.llm import ChatMessage, ChatModel
from iamia.ai.prompts import REWRITE_SYSTEM_PROMPT, build_rewrite_prompt
from iamia.conversations.models import Author, Turn
from iamia.core.metrics import AI_BLOCKED
from iamia.core.text import fold, truncate
from iamia.security.input import detect_prompt_injection, escape_for_prompt
from iamia.security.pii import mask_pii

logger = logging.getLogger(__name__)

_FOLLOW_UP = re.compile(
    r"^(y|e|pero|entonces|tambien|ademas|o sea|y si|y para|y en|y con|y cuanto|y donde|y como|y cuando)\b"
    r"|\b(eso|esa|ese|esto|esta|este|esos|esas|ahi|alli|lo mismo|la misma|el mismo|ese tramite|dicho tramite)\b"
)
_SPEAKERS = {Author.USER: "Ciudadano", Author.BOT: "Asistente", Author.OPERATOR: "Operador"}


def looks_like_follow_up(question: str) -> bool:
    # Sin los signos de apertura: "¿Y para hacerlo desde el exterior?" empieza con "y".
    folded = fold(question).lstrip("¿¡\"'«“( ")
    return len(folded.split()) <= 4 or bool(_FOLLOW_UP.search(folded))


class QueryRewriter:
    def __init__(
        self,
        chat_model: ChatModel,
        *,
        enabled: bool = True,
        max_turns: int = 3,
        max_tokens: int = 80,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._chat_model = chat_model
        self._enabled = enabled
        self._max_turns = max(1, max_turns)
        self._max_tokens = max_tokens
        self._timeout = timeout_seconds

    def _history_lines(self, history: Sequence[Turn]) -> list[str]:
        relevant = [turn for turn in history if turn.author in _SPEAKERS and turn.text]
        recent = relevant[-self._max_turns * 2 :]
        return [f"{_SPEAKERS[turn.author]}: {escape_for_prompt(truncate(turn.text, 300))}" for turn in recent]

    async def standalone(self, question: str, history: Sequence[Turn]) -> str:
        if not self._enabled or not looks_like_follow_up(question):
            return question
        if detect_prompt_injection(question):
            return question  # el motor lo bloquea sin pasar por ningún modelo, tampoco por la reescritura
        lines = self._history_lines(history)
        if not any(line.startswith("Ciudadano:") for line in lines):
            return question
        # El historial ya se guarda enmascarado; la consulta nueva todavía no: los datos
        # personales no llegan al modelo ni en la reescritura (el motor vuelve a enmascarar).
        masked, _ = mask_pii(question)
        messages = [
            ChatMessage("system", REWRITE_SYSTEM_PROMPT),
            ChatMessage("user", build_rewrite_prompt(lines, escape_for_prompt(masked))),
        ]
        try:
            completion = await asyncio.wait_for(
                self._chat_model.complete(messages, max_tokens=self._max_tokens, temperature=0.0), self._timeout
            )
        except Exception as exc:
            logger.warning("query_rewrite_failed", extra={"ctx": {"error": type(exc).__name__}})
            return question
        rewritten = guard_rewrite(completion.text)
        if rewritten is None or detect_prompt_injection(rewritten):
            AI_BLOCKED.labels("rewrite_rejected").inc()
            return question
        return rewritten
