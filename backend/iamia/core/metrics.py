"""Métricas Prometheus de toda la plataforma.

Si la API corre con varios procesos (uvicorn --workers N) se usa el modo multiproceso de
prometheus_client: cada proceso escribe en PROMETHEUS_MULTIPROC_DIR y /metrics agrega todo.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

_MULTIPROC_DIR = os.getenv("PROMETHEUS_MULTIPROC_DIR")
if _MULTIPROC_DIR:
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120, 300)
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

# --------------------------------------------------------------------- canales y webhooks
MESSAGES_RECEIVED = Counter("iamia_messages_received_total", "Mensajes entrantes aceptados", ["channel", "kind"])
MESSAGES_DUPLICATED = Counter(
    "iamia_messages_duplicated_total", "Mensajes repetidos descartados por idempotencia", ["channel"]
)
WEBHOOK_REJECTED = Counter("iamia_webhook_rejected_total", "Webhooks rechazados", ["channel", "reason"])
STATUS_UPDATES = Counter(
    "iamia_status_updates_total", "Estados de entrega informados por las plataformas", ["channel", "status"]
)

# --------------------------------------------------------------------- procesamiento
MESSAGES_PROCESSED = Counter("iamia_messages_processed_total", "Mensajes entrantes procesados", ["channel", "outcome"])
PROCESSING_SECONDS = Histogram(
    "iamia_processing_seconds",
    "Tiempo de procesamiento de un mensaje entrante",
    ["channel"],
    buckets=_LATENCY_BUCKETS,
)
END_TO_END_SECONDS = Histogram(
    "iamia_end_to_end_seconds",
    "Tiempo desde la recepción hasta el envío de la respuesta",
    ["channel"],
    buckets=_LATENCY_BUCKETS,
)
HANDOFFS = Counter("iamia_handoffs_total", "Derivaciones a atención humana", ["channel", "reason"])

# --------------------------------------------------------------------- entregas
DELIVERIES = Counter("iamia_deliveries_total", "Resultados de envío por canal", ["channel", "result"])
DELIVERY_SECONDS = Histogram(
    "iamia_delivery_seconds",
    "Latencia de la API de envío de cada plataforma",
    ["channel"],
    buckets=_HTTP_BUCKETS,
)
CIRCUIT_STATE = Gauge(
    "iamia_circuit_state",
    "Estado del circuit breaker (0 cerrado, 1 semiabierto, 2 abierto)",
    ["name"],
    multiprocess_mode="max",
)

# --------------------------------------------------------------------- colas
QUEUE_PUBLISHED = Counter("iamia_queue_published_total", "Trabajos publicados", ["queue"])
QUEUE_RETRIED = Counter("iamia_queue_retried_total", "Trabajos reprogramados", ["queue"])
QUEUE_DEAD = Counter("iamia_queue_dead_total", "Trabajos enviados a la cola de descarte (DLQ)", ["queue"])
QUEUE_REQUEUED_BY_SWEEPER = Counter(
    "iamia_sweeper_requeued_total", "Mensajes reencolados por el barrido de pendientes", ["direction"]
)
KEY_ROTATION_ROWS = Counter(
    "iamia_key_rotation_rows_total",
    "Datos recifrados con la clave activa después de una rotación (o que no se pudieron descifrar)",
    ["table", "result"],
)

# --------------------------------------------------------------------- motor de IA
AI_REQUESTS = Counter("iamia_ai_requests_total", "Consultas al motor de IA", ["outcome"])
AI_CACHE_HITS = Counter("iamia_ai_cache_hits_total", "Aciertos de caché del motor", ["layer"])
AI_BLOCKED = Counter("iamia_ai_blocked_total", "Consultas bloqueadas o cortadas", ["reason"])
AI_SECONDS = Histogram("iamia_ai_seconds", "Tiempo total del motor por consulta", buckets=_LATENCY_BUCKETS)
LLM_SECONDS = Histogram("iamia_llm_seconds", "Latencia de generación del LLM", buckets=_LATENCY_BUCKETS)
LLM_TOKENS = Counter("iamia_llm_tokens_total", "Tokens procesados por el LLM", ["kind"])
RETRIEVAL_BEST_SCORE = Histogram(
    "iamia_retrieval_best_score",
    "Similitud del mejor documento recuperado",
    buckets=(0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0),
)
GROUNDING_SCORE = Histogram(
    "iamia_grounding_score",
    "Proporción de términos de la respuesta presentes en el contexto",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# --------------------------------------------------------------------- HTTP
HTTP_REQUESTS = Counter("iamia_http_requests_total", "Pedidos HTTP", ["route", "method", "status"])
HTTP_SECONDS = Histogram("iamia_http_seconds", "Latencia HTTP", ["route"], buckets=_HTTP_BUCKETS)


def render_metrics() -> tuple[bytes, str]:
    if _MULTIPROC_DIR:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
