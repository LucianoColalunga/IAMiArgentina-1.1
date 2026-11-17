"""Motor de respuesta (RAG) independiente de los canales.

Recibe una pregunta ya independiente (sin depender del historial) y la resuelve cortando lo
antes posible para no gastar GPU: limpieza -> prompt injection -> datos personales -> caché
exacta -> caché semántica -> recuperación con umbral de relevancia -> single-flight ->
presupuesto de tokens -> circuit breaker -> compuerta de concurrencia -> generación ->
guard de salida -> anclaje en el contexto -> caché.

No sabe nada de WhatsApp, HTTP ni conversaciones: devuelve un `Answer` o lanza errores del
dominio (InvalidRequest, QuotaExceeded, ServiceBusy, ServiceUnavailable).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from opentelemetry.trace import SpanKind

from iamia.ai.caches import AnswerCache
from iamia.ai.embeddings import Embedder
from iamia.ai.grounding import GroundingPolicy
from iamia.ai.guard import guard_output
from iamia.ai.limits import LLMGate, TokenBudget
from iamia.ai.llm import ChatMessage, ChatModel, LLMError, LLMTimeout
from iamia.ai.prompts import (
    CONTEXT_BLOCK_TEMPLATE,
    CONTEXT_SEPARATOR,
    NO_INFO_MESSAGE,
    PROMPT_FINGERPRINT,
    REFUSAL_MESSAGE,
    build_system_prompt,
    build_user_prompt,
)
from iamia.core.errors import InvalidRequest, QuotaExceeded, ServiceBusy, ServiceUnavailable
from iamia.core.metrics import (
    AI_BLOCKED,
    AI_CACHE_HITS,
    AI_REQUESTS,
    AI_SECONDS,
    GROUNDING_SCORE,
    LLM_SECONDS,
    LLM_TOKENS,
    RETRIEVAL_BEST_SCORE,
)
from iamia.core.resilience import CircuitBreaker
from iamia.core.text import normalize_for_cache, stable_hash
from iamia.core.tokens import TokenCounter
from iamia.core.tracing import child_span, set_attributes
from iamia.rag.models import KBInfo, RetrievedChunk, Retriever, SemanticCache
from iamia.security.input import detect_prompt_injection, escape_for_prompt, sanitize_context, sanitize_user_input
from iamia.security.pii import mask_pii

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    ANSWERED = "answered"  # respuesta con información del contexto oficial
    NO_INFO = "no_info"  # consulta válida sin información oficial disponible
    REFUSED = "refused"  # fuera de alcance o salida bloqueada por el guard
    BLOCKED = "blocked"  # intento de prompt injection


@dataclass(frozen=True)
class Source:
    title: str
    url: str


@dataclass(frozen=True)
class Answer:
    text: str
    outcome: Outcome
    sources: tuple[Source, ...] = ()
    block_reason: str | None = None
    cache_layer: str | None = None
    kb_version: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    pii_detected: bool = False
    grounding: float | None = None
    latency_ms: int = 0

    @property
    def cached(self) -> bool:
        return self.cache_layer is not None

    def to_cache(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "outcome": self.outcome.value,
            "sources": [{"title": source.title, "url": source.url} for source in self.sources],
            "block_reason": self.block_reason,
            "kb_version": self.kb_version,
            "grounding": self.grounding,
        }

    @classmethod
    def from_cache(cls, payload: dict[str, Any], *, layer: str, pii_detected: bool, latency_ms: int) -> Answer:
        return cls(
            text=str(payload.get("text", "")),
            outcome=Outcome(payload.get("outcome", Outcome.ANSWERED.value)),
            sources=tuple(Source(str(s.get("title", "")), str(s.get("url", ""))) for s in payload.get("sources", [])),
            block_reason=payload.get("block_reason"),
            cache_layer=layer,
            kb_version=payload.get("kb_version"),
            pii_detected=pii_detected,
            grounding=payload.get("grounding"),
            latency_ms=latency_ms,
        )


@dataclass(frozen=True)
class EngineConfig:
    prompt_version: str = "v1"
    max_question_chars: int = 1000
    max_question_tokens: int = 256
    max_context_tokens: int = 2500
    max_output_tokens: int = 512
    retrieval_top_k: int = 4
    min_relevance_score: float = 0.52
    retrieval_score_margin: float = 0.08
    cache_ttl_seconds: int = 86_400
    refusal_cache_ttl_seconds: int = 3_600
    single_flight_lock_seconds: int = 90
    single_flight_wait_seconds: float = 20.0
    llm_timeout_seconds: float = 60.0
    embeddings_timeout_seconds: float = 15.0
    allowed_link_domains: tuple[str, ...] = ("argentina.gob.ar", "gob.ar")
    grounding: GroundingPolicy = field(default_factory=GroundingPolicy)


def build_context(chunks: list[RetrievedChunk], counter: TokenCounter, max_tokens: int) -> tuple[str, list[Source]]:
    """Arma el contexto oficial respetando un presupuesto máximo de tokens."""
    blocks: list[str] = []
    sources: list[Source] = []
    seen_urls: set[str] = set()
    used = 0
    for index, chunk in enumerate(chunks, start=1):
        block = CONTEXT_BLOCK_TEMPLATE.format(
            index=index,
            title=chunk.title or "Sin título",
            url=chunk.url or "sin URL",
            content=sanitize_context(chunk.text),
        )
        cost = counter.count(block)
        if used + cost > max_tokens:
            remaining = max_tokens - used
            if remaining < 80:
                break
            block, cost = counter.truncate(block, remaining), remaining
        blocks.append(block)
        used += cost
        if chunk.url and chunk.url not in seen_urls:
            seen_urls.add(chunk.url)
            sources.append(Source(title=chunk.title or chunk.url, url=chunk.url))
    return CONTEXT_SEPARATOR.join(blocks), sources


class AssistantEngine:
    def __init__(
        self,
        *,
        config: EngineConfig,
        retriever: Retriever,
        embedder: Embedder,
        chat_model: ChatModel,
        answer_cache: AnswerCache,
        token_budget: TokenBudget,
        llm_gate: LLMGate,
        breaker: CircuitBreaker,
        tokens: TokenCounter | None = None,
        semantic_cache: SemanticCache | None = None,
    ) -> None:
        self._config = config
        self._retriever = retriever
        self._embedder = embedder
        self._chat_model = chat_model
        self._cache = answer_cache
        self._semantic = semantic_cache
        self._budget = token_budget
        self._gate = llm_gate
        self._breaker = breaker
        self._tokens = tokens or TokenCounter()

    @property
    def chat_model(self) -> ChatModel:
        return self._chat_model

    def validate_question(self, raw_question: str) -> str:
        question = sanitize_user_input(raw_question)
        if not question:
            raise InvalidRequest("La consulta está vacía.")
        if len(question) > self._config.max_question_chars:
            raise InvalidRequest(f"La consulta supera el máximo de {self._config.max_question_chars} caracteres.")
        if self._tokens.count(question) > self._config.max_question_tokens:
            raise InvalidRequest("La consulta es demasiado extensa. Resumila en una o dos oraciones.")
        return question

    async def answer(self, raw_question: str, *, client_key: str) -> Answer:
        started = time.perf_counter()
        with child_span("ai.answer") as span:
            try:
                answer = await self._answer(raw_question, client_key, started)
            except Exception as exc:
                AI_REQUESTS.labels(type(exc).__name__.lower()).inc()
                raise
            set_attributes(
                span,
                **{
                    "iamia.outcome": answer.outcome.value,
                    "iamia.cache_layer": answer.cache_layer,
                    "iamia.block_reason": answer.block_reason,
                    "iamia.kb_version": answer.kb_version,
                },
            )
        AI_REQUESTS.labels(answer.outcome.value).inc()
        AI_SECONDS.observe(time.perf_counter() - started)
        logger.info(
            "ai_answer",
            extra={
                "ctx": {
                    "outcome": answer.outcome.value,
                    "cache_layer": answer.cache_layer,
                    "block_reason": answer.block_reason,
                    "kb_version": answer.kb_version,
                    "input_tokens": answer.input_tokens,
                    "output_tokens": answer.output_tokens,
                    "grounding": answer.grounding,
                    "latency_ms": answer.latency_ms,
                }
            },
        )
        return answer

    def _elapsed_ms(self, started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    async def _answer(self, raw_question: str, client_key: str, started: float) -> Answer:
        cfg = self._config
        question = self.validate_question(raw_question)

        # 1) Prompt injection y datos personales -----------------------------------
        attack_rule = detect_prompt_injection(question)
        masked, pii_types = mask_pii(question)
        pii = bool(pii_types)
        if attack_rule:
            AI_BLOCKED.labels("prompt_injection").inc()
            logger.warning("prompt_injection_blocked", extra={"ctx": {"rule": attack_rule}})
            return Answer(
                REFUSAL_MESSAGE,
                Outcome.BLOCKED,
                block_reason="prompt_injection",
                pii_detected=pii,
                latency_ms=self._elapsed_ms(started),
            )

        # 2) Caché exacta ------------------------------------------------------------
        kb = await self._active_kb()
        normalized = normalize_for_cache(masked)
        # Versión del índice, del prompt y del modelo forman parte de la clave: cualquier cambio
        # invalida solo la caché, sin tener que borrarla a mano.
        scope = stable_hash(cfg.prompt_version, PROMPT_FINGERPRINT, self._chat_model.model_name, kb.version)[:16]
        cache_key = stable_hash(scope, normalized)
        cached = await self._cache.get(cache_key)
        if cached is not None:
            AI_CACHE_HITS.labels("redis").inc()
            return Answer.from_cache(cached, layer="redis", pii_detected=pii, latency_ms=self._elapsed_ms(started))

        # 3) Embedding y caché semántica ----------------------------------------------
        try:
            with child_span("ai.embed", kind=SpanKind.CLIENT):
                embedding = await asyncio.wait_for(
                    self._embedder.embed_query(masked), timeout=cfg.embeddings_timeout_seconds
                )
        except Exception as exc:
            logger.error("embedding_failed", extra={"ctx": {"error": type(exc).__name__}})
            raise ServiceUnavailable("servicio de embeddings no disponible", retry_after=10) from exc

        if self._semantic is not None:
            similar_key = await self._semantic.lookup(embedding, scope)
            if similar_key:
                cached = await self._cache.get(similar_key)
                if cached is not None:
                    AI_CACHE_HITS.labels("semantic").inc()
                    await self._cache.set(cache_key, cached, cfg.cache_ttl_seconds)  # promoción a caché exacta
                    return Answer.from_cache(
                        cached, layer="semantic", pii_detected=pii, latency_ms=self._elapsed_ms(started)
                    )

        # 4) Recuperación con umbral de relevancia ---------------------------------------
        with child_span("ai.retrieve") as span:
            chunks = await self._search(kb, embedding)
            best = max((chunk.score for chunk in chunks), default=0.0)
            set_attributes(span, **{"iamia.chunks": len(chunks), "iamia.best_score": round(best, 4)})
        RETRIEVAL_BEST_SCORE.observe(best)
        cutoff = max(cfg.min_relevance_score, best - cfg.retrieval_score_margin)
        relevant = [chunk for chunk in chunks if chunk.score >= cutoff]
        if not relevant:
            # Nada en la base oficial se parece a la consulta: fuera de alcance, sin tocar el LLM.
            AI_BLOCKED.labels("out_of_scope").inc()
            refusal = Answer(
                REFUSAL_MESSAGE,
                Outcome.REFUSED,
                block_reason="out_of_scope",
                kb_version=kb.version,
                pii_detected=pii,
                latency_ms=self._elapsed_ms(started),
            )
            await self._cache.set(cache_key, refusal.to_cache(), cfg.refusal_cache_ttl_seconds)
            return refusal

        # 5) Single-flight ---------------------------------------------------------------
        lock_token = await self._cache.acquire_lock(cache_key, cfg.single_flight_lock_seconds)
        if lock_token is None:
            cached = await self._cache.wait_for(cache_key, cfg.single_flight_wait_seconds)
            if cached is not None:
                AI_CACHE_HITS.labels("coalesced").inc()
                return Answer.from_cache(
                    cached, layer="coalesced", pii_detected=pii, latency_ms=self._elapsed_ms(started)
                )
            # El líder falló o tardó demasiado: se genera en este worker.
        try:
            return await self._generate(masked, relevant, kb, scope, cache_key, embedding, client_key, pii, started)
        finally:
            if lock_token is not None:
                await self._cache.release_lock(cache_key, lock_token)

    async def _active_kb(self) -> KBInfo:
        try:
            kb = await self._retriever.active()
        except Exception as exc:
            logger.error("kb_unavailable", extra={"ctx": {"error": type(exc).__name__}})
            raise ServiceUnavailable("base de conocimiento no disponible", retry_after=10) from exc
        if kb is None:
            raise ServiceUnavailable("todavía no hay una base de conocimiento activa", retry_after=30)
        return kb

    async def _search(self, kb: KBInfo, embedding: list[float]) -> list[RetrievedChunk]:
        for attempt in (1, 2):
            try:
                return await self._retriever.search(kb, embedding, self._config.retrieval_top_k)
            except Exception as exc:
                if attempt == 2:
                    logger.error("retrieval_failed", extra={"ctx": {"error": type(exc).__name__}})
                    raise ServiceUnavailable("búsqueda no disponible", retry_after=10) from exc
                kb = await self._active_kb()  # la versión pudo haber cambiado por una ingesta nueva
        raise RuntimeError("inalcanzable")

    async def _generate(
        self,
        masked_question: str,
        chunks: list[RetrievedChunk],
        kb: KBInfo,
        scope: str,
        cache_key: str,
        embedding: list[float],
        client_key: str,
        pii: bool,
        started: float,
    ) -> Answer:
        cfg = self._config
        if await self._budget.remaining(client_key) <= 0:
            AI_BLOCKED.labels("token_budget").inc()
            raise QuotaExceeded(
                "Alcanzaste el límite diario de consultas al asistente. Intentá nuevamente mañana.", retry_after=3600
            )
        if not self._breaker.allow():
            AI_BLOCKED.labels("llm_circuit_open").inc()
            raise ServiceUnavailable("el modelo no está disponible", retry_after=max(5.0, self._breaker.retry_after()))

        context, sources = build_context(chunks, self._tokens, cfg.max_context_tokens)
        canary = secrets.token_hex(6)  # si aparece en la salida, hubo fuga del system prompt
        messages = [
            ChatMessage("system", build_system_prompt(canary)),
            ChatMessage("user", build_user_prompt(context, escape_for_prompt(masked_question))),
        ]
        try:
            async with self._gate.slot():
                llm_started = time.perf_counter()
                model = {"gen_ai.request.model": self._chat_model.model_name}
                with child_span("ai.llm", kind=SpanKind.CLIENT, **model):
                    completion = await asyncio.wait_for(
                        self._chat_model.complete(messages, max_tokens=cfg.max_output_tokens),
                        timeout=cfg.llm_timeout_seconds,
                    )
                LLM_SECONDS.observe(time.perf_counter() - llm_started)
        except ServiceBusy:
            AI_BLOCKED.labels("llm_busy").inc()
            raise
        except (TimeoutError, LLMTimeout) as exc:
            self._breaker.record_failure()
            logger.error("llm_timeout")
            raise ServiceBusy("el modelo tardó demasiado", retry_after=5) from exc
        except LLMError as exc:
            if exc.retryable:
                self._breaker.record_failure()
            logger.error("llm_failed", extra={"ctx": {"error": str(exc)}})
            raise ServiceUnavailable("el modelo no está disponible", retry_after=10) from exc
        self._breaker.record_success()

        input_tokens = completion.input_tokens or self._tokens.count(messages[0].content + messages[1].content)
        output_tokens = completion.output_tokens or self._tokens.count(completion.text)
        LLM_TOKENS.labels("input").inc(input_tokens)
        LLM_TOKENS.labels("output").inc(output_tokens)
        await self._budget.consume(client_key, input_tokens + output_tokens)

        guarded = guard_output(completion.text, canary, list(cfg.allowed_link_domains))
        if guarded.violation:
            AI_BLOCKED.labels(guarded.violation).inc()
            logger.warning("output_guard_triggered", extra={"ctx": {"violation": guarded.violation}})

        text, block_reason, grounding = guarded.text, guarded.violation, None
        if guarded.is_refusal:
            outcome = Outcome.REFUSED
            block_reason = block_reason or "out_of_scope"
        elif guarded.is_no_info:
            outcome = Outcome.NO_INFO
        else:
            outcome = Outcome.ANSWERED
            grounding, accepted = cfg.grounding.evaluate(text, context)
            if grounding is not None:
                GROUNDING_SCORE.observe(grounding)
            if not accepted:
                AI_BLOCKED.labels("low_grounding").inc()
                text, outcome, block_reason = NO_INFO_MESSAGE, Outcome.NO_INFO, "low_grounding"

        answer = Answer(
            text=text,
            outcome=outcome,
            sources=tuple(sources) if outcome is Outcome.ANSWERED else (),
            block_reason=block_reason,
            kb_version=kb.version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pii_detected=pii,
            grounding=grounding,
            latency_ms=self._elapsed_ms(started),
        )
        fallback = outcome is not Outcome.ANSWERED
        ttl = cfg.refusal_cache_ttl_seconds if (fallback or block_reason) else cfg.cache_ttl_seconds
        await self._cache.set(cache_key, answer.to_cache(), ttl)
        if self._semantic is not None and not fallback and not block_reason:
            await self._semantic.store(embedding, scope, cache_key)
        return answer
