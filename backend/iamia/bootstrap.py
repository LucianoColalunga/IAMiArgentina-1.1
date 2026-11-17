"""Composition root: arma cada componente a partir de la configuración.

Es el único módulo que conoce las clases concretas de todo el sistema. El resto recibe sus
dependencias por constructor, lo que permite probar cada pieza con dobles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg
from redis.asyncio import Redis

from iamia.ai.caches import AnswerCache
from iamia.ai.embeddings import OpenAICompatibleEmbedder
from iamia.ai.engine import AssistantEngine, EngineConfig
from iamia.ai.grounding import GroundingPolicy
from iamia.ai.limits import LLMGate, TokenBudget
from iamia.ai.llm import OpenAICompatibleChatModel
from iamia.channels.models import Channel
from iamia.channels.registry import ChannelRegistry
from iamia.conversations.context import QueryRewriter
from iamia.conversations.manager import ConversationManager, ManagerConfig
from iamia.conversations.throttle import RedisContactThrottle
from iamia.core.resilience import CircuitBreaker
from iamia.integrations.factory import build_channel_registry
from iamia.messaging.contracts import JobQueue
from iamia.messaging.rabbitmq import RabbitJobQueue
from iamia.messaging.throttle import ChannelThrottle
from iamia.persistence.codec import DataCodec
from iamia.persistence.console import ConsoleRepository
from iamia.persistence.contacts import AuditRepository, ContactRepository
from iamia.persistence.database import create_pool
from iamia.persistence.inbox import InboxRepository
from iamia.persistence.maintenance import MaintenanceRepository, RetentionPolicy
from iamia.persistence.oauth import PgTokenStore
from iamia.persistence.rotation import KeyRotationRepository
from iamia.persistence.webchat import WebchatRepository
from iamia.persistence.work import WorkRepository
from iamia.rag.store import PgVectorKnowledgeBase, PgVectorSemanticCache
from iamia.security.auth import JwtVerifier, TokenVerifier
from iamia.security.crypto import FieldCipher, Pseudonymizer
from iamia.security.session_tokens import SessionTokens
from iamia.services.console import ConsoleService
from iamia.services.ingest import IngestService
from iamia.services.publisher import JobPublisher
from iamia.settings import Settings
from iamia.workers.inbound import InboundConfig, InboundProcessor
from iamia.workers.maintenance import MaintenanceConfig, MaintenanceRunner
from iamia.workers.outbound import OutboundConfig, OutboundDispatcher

logger = logging.getLogger(__name__)


def _secret(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    return getter() if getter else str(value or "")


def check_security(settings: Settings) -> None:
    problems = settings.security_problems()
    for problem in problems:
        logger.warning("insecure_configuration", extra={"ctx": {"problem": problem}})
    if problems and settings.is_production:
        raise RuntimeError("configuración insegura en producción: " + "; ".join(problems))


def build_engine(settings: Settings, pool: asyncpg.Pool, redis: Redis) -> AssistantEngine:
    config = EngineConfig(
        prompt_version=settings.prompt_version,
        max_question_chars=settings.max_question_chars,
        max_question_tokens=settings.max_question_tokens,
        max_context_tokens=settings.max_context_tokens,
        max_output_tokens=settings.max_output_tokens,
        retrieval_top_k=settings.retrieval_top_k,
        min_relevance_score=settings.min_relevance_score,
        retrieval_score_margin=settings.retrieval_score_margin,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        refusal_cache_ttl_seconds=settings.refusal_cache_ttl_seconds,
        single_flight_lock_seconds=settings.single_flight_lock_seconds,
        single_flight_wait_seconds=settings.single_flight_wait_seconds,
        llm_timeout_seconds=settings.llm_timeout_seconds,
        embeddings_timeout_seconds=settings.embeddings_timeout_seconds,
        allowed_link_domains=tuple(settings.allowed_link_domains_list),
        grounding=GroundingPolicy(settings.grounding_mode, settings.grounding_min_score),
    )
    semantic = (
        PgVectorSemanticCache(
            pool,
            dimensions=settings.embedding_dimensions,
            threshold=settings.semantic_cache_threshold,
            ttl_seconds=settings.semantic_cache_ttl_seconds,
        )
        if settings.semantic_cache_enabled
        else None
    )
    return AssistantEngine(
        config=config,
        retriever=PgVectorKnowledgeBase(
            pool,
            refresh_seconds=settings.kb_refresh_seconds,
            ef_search=settings.hnsw_ef_search,
            expected_embed_model=settings.embeddings_model,
        ),
        embedder=OpenAICompatibleEmbedder(
            base_url=settings.embeddings_base_url,
            model=settings.embeddings_model,
            api_key=_secret(settings.embeddings_api_key),
            dimensions=settings.embedding_dimensions,
            timeout=settings.embeddings_timeout_seconds,
        ),
        chat_model=OpenAICompatibleChatModel(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=_secret(settings.llm_api_key),
            temperature=settings.llm_temperature,
            max_tokens=settings.max_output_tokens,
            timeout=settings.llm_timeout_seconds,
        ),
        answer_cache=AnswerCache(redis),
        token_budget=TokenBudget(redis, settings.token_budget_per_client_per_day),
        llm_gate=LLMGate(settings.max_concurrent_llm_calls, settings.llm_queue_timeout_seconds),
        breaker=CircuitBreaker("llm", settings.llm_breaker_failures, settings.llm_breaker_reset_seconds),
        semantic_cache=semantic,
    )


@dataclass
class Container:
    settings: Settings
    pool: asyncpg.Pool
    redis: Redis
    queue: JobQueue
    codec: DataCodec
    registry: ChannelRegistry
    engine: AssistantEngine
    inbox: InboxRepository
    work: WorkRepository
    contacts: ContactRepository
    audit: AuditRepository
    console_repo: ConsoleRepository
    maintenance_repo: MaintenanceRepository
    webchat_repo: WebchatRepository
    publisher: JobPublisher
    ingest: IngestService
    console: ConsoleService
    manager: ConversationManager
    owns_queue: bool = True
    _closed: bool = field(default=False, repr=False)

    @classmethod
    async def create(cls, settings: Settings, *, queue: JobQueue | None = None, role: str = "app") -> Container:
        check_security(settings)
        codec = DataCodec(
            FieldCipher.from_config(_secret(settings.data_encryption_keys)),
            Pseudonymizer.from_config(_secret(settings.pseudonymization_key)),
        )
        pool = await create_pool(
            _secret(settings.database_url),
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            statement_timeout_ms=settings.db_statement_timeout_ms,
            application_name=f"iamia-{role}",
        )
        redis = Redis.from_url(_secret(settings.redis_url), decode_responses=True, health_check_interval=30)
        owns_queue = queue is None
        if queue is None:
            rabbit = RabbitJobQueue(_secret(settings.amqp_url), max_length=settings.queue_max_length)
            await rabbit.connect()
            queue = rabbit

        inbox = InboxRepository(pool, codec, idle_minutes=settings.conversation_idle_minutes)
        work = WorkRepository(pool, codec, context_turns=settings.context_turns)
        contacts = ContactRepository(pool)
        audit = AuditRepository(pool)
        publisher = JobPublisher(queue, inbox.mark_enqueued)
        registry = build_channel_registry(settings, redis=redis, token_store=PgTokenStore(pool, codec))
        engine = build_engine(settings, pool, redis)
        manager = ConversationManager(
            engine=engine,
            rewriter=QueryRewriter(
                engine.chat_model,
                enabled=settings.context_rewrite_enabled,
                max_turns=settings.context_turns,
                max_tokens=settings.context_rewrite_max_tokens,
            ),
            contacts=contacts,
            audit=audit,
            throttle=RedisContactThrottle(
                redis, per_minute=settings.contact_messages_per_minute, per_day=settings.contact_messages_per_day
            ),
            code_hasher=codec.link_code_hash,
            config=ManagerConfig(
                handoff_enabled=settings.handoff_enabled,
                handoff_after_no_info=settings.handoff_after_no_info,
                identity_linking_enabled=settings.identity_linking_enabled,
                retention_messages_days=settings.retention_messages_days,
            ),
        )
        console_repo = ConsoleRepository(pool, codec)
        return cls(
            settings=settings,
            pool=pool,
            redis=redis,
            queue=queue,
            codec=codec,
            registry=registry,
            engine=engine,
            inbox=inbox,
            work=work,
            contacts=contacts,
            audit=audit,
            console_repo=console_repo,
            maintenance_repo=MaintenanceRepository(pool),
            webchat_repo=WebchatRepository(pool, codec),
            publisher=publisher,
            ingest=IngestService(inbox, publisher, redis, max_age_seconds=settings.inbound_max_age_seconds),
            console=ConsoleService(console_repo, work, audit, publisher),
            manager=manager,
            owns_queue=owns_queue,
        )

    # ------------------------------------------------------------------ piezas por rol
    def inbound_processor(self) -> InboundProcessor:
        s = self.settings
        return InboundProcessor(
            work=self.work,
            manager=self.manager,
            registry=self.registry,
            queue=self.queue,
            publisher=JobPublisher(self.queue, self.inbox.mark_enqueued),
            config=InboundConfig(
                lease_seconds=s.processing_lease_seconds,
                max_attempts=s.inbound_max_attempts,
                max_age_seconds=s.inbound_max_age_seconds,
            ),
        )

    def outbound_dispatcher(self) -> OutboundDispatcher:
        s = self.settings
        limits = s.channel_rate_limit_map
        breakers = {
            channel: CircuitBreaker(
                f"channel:{channel.value}", s.channel_breaker_failures, s.channel_breaker_reset_seconds
            )
            for channel in self.registry.channels
        }
        throttles = {
            channel: ChannelThrottle(self.redis, channel.value, limits[channel.value])
            for channel in self.registry.channels
            if channel.value in limits
        }
        return OutboundDispatcher(
            work=self.work,
            registry=self.registry,
            queue=self.queue,
            breakers=breakers,
            throttles=throttles,
            config=OutboundConfig(lease_seconds=s.processing_lease_seconds, max_attempts=s.outbound_max_attempts),
        )

    def maintenance_runner(self) -> MaintenanceRunner:
        s = self.settings
        semantic = (
            PgVectorSemanticCache(
                self.pool,
                dimensions=s.embedding_dimensions,
                threshold=s.semantic_cache_threshold,
                ttl_seconds=s.semantic_cache_ttl_seconds,
            )
            if s.semantic_cache_enabled
            else None
        )
        return MaintenanceRunner(
            repository=self.maintenance_repo,
            work=self.work,
            publisher=self.publisher,
            audit=self.audit,
            semantic_cache=semantic,
            rotation=KeyRotationRepository(self.pool, self.codec, grace_seconds=s.key_rotation_grace_minutes * 60),
            config=MaintenanceConfig(
                sweep_interval_seconds=s.sweeper_interval_seconds,
                housekeeping_interval_seconds=s.maintenance_interval_seconds,
                handoff_timeout_minutes=s.handoff_timeout_minutes,
                retention=RetentionPolicy(
                    messages_days=s.retention_messages_days,
                    contacts_days=s.retention_contacts_days,
                    audit_days=s.retention_audit_days,
                ),
            ),
        )

    def token_verifier(self) -> TokenVerifier | None:
        s = self.settings
        if not (s.oidc_jwks_url or s.oidc_public_key_path):
            return None
        try:
            return JwtVerifier.from_settings(
                issuer=s.oidc_issuer,
                audience=s.oidc_audience,
                algorithms=s.oidc_algorithms_list,
                jwks_url=s.oidc_jwks_url,
                public_key_path=s.oidc_public_key_path,
                roles_claim=s.oidc_roles_claim,
            )
        except (OSError, ValueError) as exc:
            # Sin clave no hay acceso a la consola (todo pedido recibe 401), pero la API sigue
            # atendiendo webhooks y el chat web: un error de configuración del panel interno no
            # puede cortar la atención a la ciudadanía.
            logger.error("oidc_key_unavailable", extra={"ctx": {"error": type(exc).__name__}})
            return None

    def session_tokens(self) -> SessionTokens | None:
        secret = _secret(self.settings.webchat_token_secret)
        return SessionTokens(secret, self.settings.webchat_session_ttl_seconds) if secret else None

    def channels(self) -> tuple[Channel, ...]:
        return self.registry.channels

    async def ping_database(self) -> bool:
        return await self.inbox.ping()

    async def ping_redis(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.registry.aclose()
        for closer in (getattr(self.engine.chat_model, "aclose", None),):
            if closer is not None:
                await closer()
        if self.owns_queue:
            await self.queue.close()
        await self.redis.aclose()
        await self.pool.close()
