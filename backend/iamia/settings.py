"""Configuración de la plataforma.

Todo se lee de variables de entorno (o de un archivo .env). Los secretos usan SecretStr para
que nunca aparezcan en logs ni en reprs. Cada componente recibe solo la parte de la
configuración que necesita (ver los dataclasses de configuración de cada módulo).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


def csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_rate_limits(value: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """Convierte "x=15/900;1440/86400,whatsapp=80" en {"x": ((15, 900), (1440, 86400)), "whatsapp": ((80, 1),)}."""
    limits: dict[str, tuple[tuple[int, int], ...]] = {}
    for item in csv_list(value):
        name, _, spec = item.partition("=")
        windows: list[tuple[int, int]] = []
        for window in spec.split(";"):
            count, _, seconds = window.strip().partition("/")
            if count.strip().isdigit() and (not seconds.strip() or seconds.strip().isdigit()):
                windows.append((int(count), int(seconds or 1)))
        if name.strip() and windows:
            limits[name.strip().lower()] = tuple(windows)
    return limits


# Las del código y las que trae docker-compose.yml para levantar el entorno local.
DEFAULT_PASSWORDS = frozenset({"iamia", "iamia-local"})


def _password(url: SecretStr) -> str | None:
    try:
        return urlsplit(_secret(url)).password
    except ValueError:
        return None


def _secret(value: SecretStr) -> str:
    return value.get_secret_value()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------------ general
    app_env: Environment = "production"
    log_level: str = "INFO"
    public_base_url: str = ""
    web_concurrency: int = 1  # en Kubernetes se escala con réplicas; con más procesos, definir PROMETHEUS_MULTIPROC_DIR

    # ------------------------------------------------------------------ PostgreSQL
    database_url: SecretStr = SecretStr("postgresql://iamia:iamia@postgres:5432/iamia")
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_statement_timeout_ms: int = 5000

    # ------------------------------------------------------------------ Redis
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")

    # ------------------------------------------------------------------ RabbitMQ
    amqp_url: SecretStr = SecretStr("amqp://iamia:iamia@rabbitmq:5672/iamia")
    queue_max_length: int = 500_000
    inbound_prefetch: int = 8
    outbound_prefetch: int = 16
    inbound_max_attempts: int = 4
    outbound_max_attempts: int = 8
    processing_lease_seconds: int = 180

    # ------------------------------------------------------------------ modelo de lenguaje
    # Cualquier servidor con API compatible con OpenAI: vLLM (producción), llama.cpp (CPU), Ollama.
    llm_base_url: str = "http://llm:8000/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8"
    llm_temperature: float = 0.1
    llm_timeout_seconds: float = 60
    max_output_tokens: int = 512
    max_concurrent_llm_calls: int = 32
    llm_queue_timeout_seconds: float = 10
    llm_breaker_failures: int = 5
    llm_breaker_reset_seconds: float = 30

    # ------------------------------------------------------------------ embeddings
    embeddings_base_url: str = "http://embeddings:8000/v1"
    embeddings_api_key: SecretStr = SecretStr("")
    embeddings_model: str = "BAAI/bge-m3"
    embedding_dimensions: int = 1024
    embeddings_timeout_seconds: float = 15

    # ------------------------------------------------------------------ RAG
    retrieval_top_k: int = 4
    min_relevance_score: float = 0.52
    retrieval_score_margin: float = 0.08
    max_context_tokens: int = 2500
    kb_refresh_seconds: float = 30
    hnsw_ef_search: int = 64
    grounding_mode: Literal["off", "observe", "enforce"] = "observe"
    grounding_min_score: float = 0.35

    # ------------------------------------------------------------------ tokens y cupos
    max_question_chars: int = 1000
    max_question_tokens: int = 256
    token_budget_per_client_per_day: int = 100_000
    rate_limit_per_minute: int = 20
    contact_messages_per_minute: int = 8
    contact_messages_per_day: int = 300

    # ------------------------------------------------------------------ cachés
    cache_ttl_seconds: int = 86_400
    refusal_cache_ttl_seconds: int = 3_600
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95
    semantic_cache_ttl_seconds: int = 86_400
    single_flight_lock_seconds: int = 90
    single_flight_wait_seconds: float = 20
    edge_cache_max_age: int = 3600
    prompt_version: str = "v1"

    # ------------------------------------------------------------------ conversaciones
    conversation_idle_minutes: int = 30
    context_turns: int = 3
    context_rewrite_enabled: bool = True
    context_rewrite_max_tokens: int = 80
    handoff_enabled: bool = True
    handoff_after_no_info: int = 2
    handoff_timeout_minutes: int = 240
    inbound_max_age_seconds: int = 86_400
    identity_linking_enabled: bool = True

    # ------------------------------------------------------------------ canales
    channels_enabled: str = "webchat"
    # Ventanas por canal: "cantidad/segundos", varias separadas por ";". Valores por defecto según los
    # límites publicados por cada plataforma (WhatsApp 80 mps por número, Telegram 30/s por bot, X 15
    # cada 15 min y 1440 por día). Messenger e Instagram quedan por debajo del máximo de la Send API.
    channel_rate_limits: str = (
        "whatsapp=80/1,telegram=25/1,messenger=40/1,instagram=40/1,x=15/900;1440/86400,sms=10/1,email=10/1"
    )
    channel_breaker_failures: int = 5
    channel_breaker_reset_seconds: float = 30
    channel_http_timeout_seconds: float = 15

    meta_graph_api_version: str = "v27.0"
    meta_graph_base_url: str = "https://graph.facebook.com"
    meta_appsecret_proof: bool = True

    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_reengagement_template: str = ""
    whatsapp_template_language: str = "es_AR"
    whatsapp_typing_indicator: bool = True

    instagram_account_id: str = ""
    instagram_access_token: SecretStr = SecretStr("")
    instagram_app_secret: SecretStr = SecretStr("")
    instagram_verify_token: SecretStr = SecretStr("")
    instagram_human_agent_tag: bool = False

    messenger_page_id: str = ""
    messenger_page_access_token: SecretStr = SecretStr("")
    messenger_app_secret: SecretStr = SecretStr("")
    messenger_verify_token: SecretStr = SecretStr("")
    messenger_human_agent_tag: bool = False

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_api_base_url: str = "https://api.telegram.org"

    x_api_base_url: str = "https://api.x.com"
    x_account_id: str = ""
    x_webhook_secret: SecretStr = SecretStr("")
    x_token_mode: Literal["refresh", "static"] = "refresh"  # noqa: S105 - modo, no secreto
    x_client_id: str = ""
    x_client_secret: SecretStr = SecretStr("")
    x_user_access_token: SecretStr = SecretStr("")  # solo modo static
    x_oauth_redirect_uri: str = ""
    x_token_url: str = "https://api.x.com/2/oauth2/token"  # noqa: S105 - URL pública
    x_authorize_url: str = "https://x.com/i/oauth2/authorize"

    webchat_token_secret: SecretStr = SecretStr("")
    webchat_session_ttl_seconds: int = 86_400
    webchat_long_poll_seconds: float = 25
    webchat_sessions_per_ip_per_hour: int = 30

    email_inbound_secret: SecretStr = SecretStr("")
    email_from_address: str = ""
    email_from_name: str = "Asistente de trámites"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 20

    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_from_number: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_api_base_url: str = "https://api.twilio.com"
    sms_webhook_url: str = ""
    sms_max_segments: int = 3

    # ------------------------------------------------------------------ seguridad
    data_encryption_keys: SecretStr = SecretStr("")
    pseudonymization_key: SecretStr = SecretStr("")
    oidc_issuer: str = ""
    oidc_audience: str = "iamia-admin"
    oidc_jwks_url: str = ""
    oidc_public_key_path: str = ""
    oidc_roles_claim: str = "realm_access.roles"
    oidc_algorithms: str = "RS256,ES256"
    edge_shared_secret: SecretStr = SecretStr("")
    trust_proxy_headers: bool = True
    cors_allow_origins: str = ""
    allowed_link_domains: str = "argentina.gob.ar,gob.ar"
    admin_api_enabled: bool = True
    webchat_ui_enabled: bool = True
    console_ui_enabled: bool = True

    # ------------------------------------------------------------------ retención de datos
    retention_messages_days: int = 90
    retention_audit_days: int = 365
    retention_contacts_days: int = 180

    # ------------------------------------------------------------------ workers
    metrics_port: int = 9100
    # Trazas de OpenTelemetry: vacío = desactivadas. Ejemplo: http://otel-collector:4318
    otel_exporter_otlp_endpoint: str = ""
    otel_traces_sample_ratio: float = 1.0
    sweeper_interval_seconds: float = 30
    maintenance_interval_seconds: float = 3600
    # Después de rotar DATA_ENCRYPTION_KEYS: margen para que termine el despliegue antes de la
    # vuelta de recifrado que confirma que no quedó nada con la clave anterior.
    key_rotation_grace_minutes: int = 60

    # ------------------------------------------------------------------ derivados
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def channels_enabled_list(self) -> list[str]:
        return [name.lower() for name in csv_list(self.channels_enabled)]

    @property
    def channel_rate_limit_map(self) -> dict[str, tuple[tuple[int, int], ...]]:
        return parse_rate_limits(self.channel_rate_limits)

    @property
    def cors_origins_list(self) -> list[str]:
        return csv_list(self.cors_allow_origins)

    @property
    def allowed_link_domains_list(self) -> list[str]:
        return [domain.lower() for domain in csv_list(self.allowed_link_domains)]

    @property
    def oidc_algorithms_list(self) -> list[str]:
        return csv_list(self.oidc_algorithms)

    def security_problems(self) -> list[str]:
        """Configuraciones inseguras. En producción impiden arrancar; en otros entornos se avisan."""
        problems: list[str] = []
        if not _secret(self.data_encryption_keys):
            problems.append("DATA_ENCRYPTION_KEYS no está definida")
        if not _secret(self.pseudonymization_key):
            problems.append("PSEUDONYMIZATION_KEY no está definida")
        urls = {"DATABASE_URL": self.database_url, "REDIS_URL": self.redis_url, "AMQP_URL": self.amqp_url}
        for name, url in urls.items():
            if _password(url) in DEFAULT_PASSWORDS:
                problems.append(f"{name} usa la contraseña por defecto")
        if self.admin_api_enabled and not (self.oidc_jwks_url or self.oidc_public_key_path):
            problems.append("la API de administración está habilitada sin OIDC_JWKS_URL ni OIDC_PUBLIC_KEY_PATH")
        if self.admin_api_enabled and self.oidc_jwks_url and not self.oidc_issuer:
            problems.append("OIDC_ISSUER es obligatorio cuando se usa OIDC_JWKS_URL")
        required = {
            "whatsapp": ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"),
            "instagram": ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_APP_SECRET", "INSTAGRAM_VERIFY_TOKEN"),
            "messenger": ("MESSENGER_PAGE_ACCESS_TOKEN", "MESSENGER_APP_SECRET", "MESSENGER_VERIFY_TOKEN"),
            "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"),
            "x": ("X_WEBHOOK_SECRET", "X_USER_ACCESS_TOKEN" if self.x_token_mode == "static" else "X_CLIENT_ID"),  # noqa: S105
            "webchat": ("WEBCHAT_TOKEN_SECRET",),
            "email": ("EMAIL_INBOUND_SECRET",),
            "sms": ("TWILIO_AUTH_TOKEN",),
        }
        for channel in self.channels_enabled_list:
            for variable in required.get(channel, ()):
                value = getattr(self, variable.lower())
                if not (_secret(value) if isinstance(value, SecretStr) else value):
                    problems.append(f"el canal {channel} está habilitado sin {variable}")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
