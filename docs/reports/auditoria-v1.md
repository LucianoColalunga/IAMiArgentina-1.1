# Auditoría de la versión 1

Revisión de la v1 (repositorio `mi-argentina-ia`, rama `main`, revisada en modo solo lectura) como
punto de partida de la 1.1: qué tenía, qué se conserva y qué le faltaba para atender a escala
nacional por varios canales. Cada brecha indica qué se hizo en la 1.1.

## Qué es la v1

Una prueba de concepto de asistente de preguntas frecuentes sobre Mi Argentina con modelos
locales.

| Parte | Cómo estaba resuelta |
|---|---|
| API | FastAPI con un solo endpoint de consulta, `POST /v1/chat`: una pregunta, una respuesta, dentro del mismo pedido HTTP. Gunicorn con 2 workers Uvicorn detrás de nginx |
| Motor | RAG armado con LangChain (clientes de chat y embeddings, divisor de texto): ChromaDB 1.5.9 como base vectorial, embeddings bge-m3, Llama 3.1 8B Instruct servido por Ollama 0.34.3 o vLLM v0.30.0 (y llama.cpp en CPU para la demo) |
| Caché y límites | Redis, solo en la red interna, sin contraseña y sin persistencia (todo con TTL) |
| Borde | nginx con límite de 5 pedidos/s por IP; Cloudflare Worker opcional que cachea `/v1/chat` en el borde y se identifica con un secreto compartido (`X-Edge-Auth`) |
| Base de conocimiento | 25 FAQs de demostración y una lista de páginas oficiales para descargar |
| Seguridad | reglas contra manipulación del prompt, enmascarado de datos personales, enlaces solo a dominios oficiales, cabeceras de seguridad, contenedores sin root, de solo lectura y sin capabilities |
| Observabilidad | métricas de Prometheus y logs |
| Pruebas | 37 automáticas (14 de la API, 15 de seguridad, 3 de ingesta, 3 del cliente del modelo, 2 de integración con Chroma), scripts de punta a punta con un Ollama simulado y una prueba de carga con Locust |
| Tamaño | ~2.300 líneas de Python |

El README de la v1 ya la presentaba como prueba de concepto independiente, no como servicio
oficial, y listaba sus propios pendientes: detección de manipulación solo por reglas, calidad sin
validar con preguntas reales, FAQs de demostración, un solo turno a propósito y sin streaming.

## Lo que se conserva

- El enfoque: responder solo con la base de conocimiento oficial, con umbral de relevancia y la
  fuente de cada respuesta.
- Modelos en servidores propios detrás de una API compatible con OpenAI: ninguna consulta va a
  servicios de IA de terceros.
- Las reglas contra manipulación, el enmascarado y la lista de dominios permitidos (ampliados).
- El contrato de `POST /v1/chat`, para no romper a quien ya lo use, y el secreto del borde.
- El endurecimiento de los contenedores (uid 10001, solo lectura, sin capabilities).
- Las 25 FAQs de demostración.

## Tecnologías: conservar, actualizar o reemplazar

| Tecnología de la v1 | En la 1.1 | Por qué |
|---|---|---|
| FastAPI | se conserva (0.163) | sirve igual para webhooks, chat web, consola y `/v1/chat`; asincrónica y con validación de tipos |
| LangChain (`langchain-core`, `-ollama`, `-openai`, `-text-splitters`) | se reemplaza por un cliente propio de la API `/v1` compatible con OpenAI (httpx) , un troceado propio (fragmentos de 1000 caracteres con 150 de solapamiento) y conteo de tokens con tiktoken para el presupuesto de contexto | la v1 lo usaba solo como envoltorio del cliente y para dividir texto. Sin él hay cuatro dependencias menos que vigilar, y timeouts, reintentos, circuit breaker y presupuesto de contexto quedan bajo control directo. Las interfaces propias (`ai/llm.py`, `ai/embeddings.py`) cumplen la función de abstracción |
| Ollama | se reemplaza: vLLM en producción, llama.cpp en CPU | vLLM agrupa pedidos de forma continua, reutiliza la caché de prefijos, aprovecha varias GPU y expone métricas. Para la demo en CPU, llama.cpp ofrece la misma API con modelos cuantizados |
| vLLM | se conserva y se actualiza (v0.30.0 → v0.33.0, fijado por digest) | es el servidor de inferencia de producción |
| Llama 3.1 8B Instruct | se reemplaza por Qwen3-30B-A3B-Instruct-2507 en FP8 (producción) y Llama 3.2 3B cuantizado (demo en CPU) | mezcla de expertos: unos 3.300 millones de parámetros activos por token, con costo parecido al de un 8B y más calidad; entra en una GPU de 48 GB ([ADR 0006](../architecture/decisiones/0006-modelos-locales.md)) |
| BGE-M3 | se conserva | embeddings multilingües de 1024 dimensiones, buenos en español; en CPU se usa cuantizado (Q8_0) |
| ChromaDB | se reemplaza por PostgreSQL + pgvector | una sola base para conversaciones, conocimiento y caché semántica, con transacciones, réplicas y respaldos (CloudNativePG) y cambio de versión azul/verde ([ADR 0003](../architecture/decisiones/0003-postgresql-pgvector.md)) |
| Redis | se conserva y se actualiza (8.6), con contraseña | caché exacta, límites e idempotencia rápida. Si se pierde su contenido no se pierde nada: PostgreSQL respalda la idempotencia y la caché se reconstruye |

## Brechas encontradas

Severidad pensada para un servicio nacional, no para la demo que la v1 se proponía ser.

| # | Severidad | Brecha | Por qué importa | Qué se hizo en la 1.1 |
|---|---|---|---|---|
| 1 | Alta | Un solo canal (HTTP) y un solo turno | la gente escribe por WhatsApp, redes, correo y SMS, y en conversación | 8 canales con un adaptador cada uno y gestor de conversaciones con contexto de los últimos turnos |
| 2 | Alta | Procesamiento sincrónico dentro del pedido | en un pico, las respuestas lentas del modelo ocupan todos los workers y la API deja de contestar; las plataformas exigen confirmar el webhook en segundos | la API solo valida, guarda y publica; RabbitMQ con colas quorum; workers que escalan con el largo de la cola |
| 3 | Alta | Nada se guarda ni es idempotente | un reinicio pierde lo que estaba en curso y un reintento de la plataforma se responde dos veces | se guarda antes de publicar, bandeja de salida con sweeper, leases, deduplicación en Redis y en PostgreSQL |
| 4 | Alta | Sin derivación a personas | lo que el asistente no sabe queda sin respuesta | estados de conversación, pedido explícito (`HUMANO`) y derivación automática tras dos consultas sin información; consola de operadores |
| 5 | Media | Redis y ChromaDB sin autenticación | dependen solo de que la red interna no se vea comprometida | contraseña en todos los servicios internos; en Kubernetes, NetworkPolicy que niega todo por defecto; ChromaDB reemplazado por pgvector |
| 6 | Media | Sin datos en reposo que proteger, pero tampoco el mecanismo | al guardar conversaciones aparecen datos personales | AES-256-GCM por campo con anillo de claves y recifrado en segundo plano; seudónimos HMAC para buscar |
| 7 | Media | Base vectorial sin réplica ni respaldo | una falla de disco obliga a reindexar todo | PostgreSQL + pgvector con CloudNativePG (3 instancias, respaldo continuo); base de conocimiento versionada azul/verde |
| 8 | Media | Sin trazas distribuidas | no hay forma de seguir un pedido entre procesos | OpenTelemetry con el id del mensaje como id de traza |
| 9 | Media | Despliegue solo con Compose | sin alta disponibilidad ni escalado automático | manifiestos de Kubernetes con overlays por entorno, KEDA y HPA, PodDisruptionBudget y NetworkPolicy |
| 10 | Media | Sin gestión de identidades ni auditoría | (no había consola) | OIDC con verificación del JWT en la API, cuatro roles y auditoría de cada acción |
| 11 | Baja | Detección de manipulación solo por reglas | no es infalible | se mantiene dentro de varias capas (relevancia, control de salida, anclaje); un clasificador dedicado sigue pendiente |
| 12 | Baja | FAQs de demostración | respuestas limitadas | sigue igual: la base definitiva necesita un responsable y fecha de revisión de cada contenido |
| 13 | Baja | Sin pruebas de canales, resiliencia ni carga de punta a punta | no se sabe cómo se comporta ante fallas | 784 unitarias, 45 de integración, 41 de punta a punta, pruebas de carga y de resiliencia ([resultados](resultados-pruebas.md)) |

Ninguno de estos puntos es un error de la v1 dentro de su alcance: es lo que separa una prueba de
concepto de un servicio que atiende a millones de personas.

## Cómo se revisó

- Clon del repositorio en solo lectura, sin commits ni cambios en su historial.
- Lectura del código, la configuración de Docker Compose y nginx, el Worker de Cloudflare, las
  pruebas y el README.
- Las versiones e imágenes citadas salen de los archivos de Compose de la v1.
