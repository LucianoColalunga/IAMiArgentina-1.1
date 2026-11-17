# Estructura del código

## El repositorio

```text
IAMiArgentina-1.1/
  backend/                 la aplicación (paquete iamia) y sus pruebas
    iamia/                 código: api, worker, dispatcher, mantenimiento, CLI
    tests/                 unitarias e integración
    Dockerfile             imagen única para todos los roles
    pyproject.toml         configuración de ruff y pytest
    requirements.txt       dependencias fijadas por versión
  frontend/
    webchat/               chat web: HTML, CSS y JavaScript sin dependencias
    console/               consola de operadores: HTML, CSS y JavaScript sin dependencias
  data/
    faqs/                  FAQs de demostración (JSON y guías en Markdown)
    sources.txt            páginas oficiales para la ingesta
  infrastructure/
    docker/                imágenes propias de PostgreSQL y configuración de RabbitMQ
    nginx/                 borde: rutas, límites y cabeceras
    monitoring/            Prometheus, alertas, Grafana, Loki, Alloy, OpenTelemetry y Tempo
    kubernetes/            base y overlays de staging y producción (kustomize)
  tests/
    e2e/                   punta a punta con simuladores de las plataformas y del modelo
    load/                  pruebas de carga
  scripts/                 e2e, carga y puente del servidor de correo
  docs/                    documentación, informes y este documento (docs/pdf)
  docker-compose.yml       núcleo; compose.cpu.yml, compose.gpu.yml y
                           compose.observability.yml se suman según el caso
```

## Módulos de la aplicación

| Módulo | Responsabilidad | Archivos | Líneas |
|---|---|---|---|
| `api` | aplicación FastAPI: rutas de webhooks, chat web, `/v1/chat`, consola y salud; middleware y errores | 11 | 801 |
| `channels` | modelo común de mensajes, contratos de los adaptadores, formato y registro de canales | 5 | 482 |
| `integrations` | un adaptador por plataforma: WhatsApp, Messenger, Instagram, Telegram, X, SMS, correo y chat web | 23 | 2.295 |
| `conversations` | gestor de conversaciones: estados, comandos, derivación, contexto y textos | 8 | 649 |
| `ai` | motor de respuesta, cliente de los modelos, prompts, control de salida, anclaje y límites | 9 | 1.136 |
| `rag` | búsqueda en la base de conocimiento, caché semántica e ingesta | 9 | 842 |
| `messaging` | colas en RabbitMQ: publicación, consumo, reintentos y descarte | 6 | 494 |
| `persistence` | acceso a PostgreSQL por agregado y migraciones | 11 | 1.592 |
| `security` | cifrado, seudonimización, datos personales, firmas, OIDC, RBAC y sesiones | 8 | 673 |
| `services` | casos de uso de la consola y de la ingesta | 4 | 304 |
| `workers` | los procesos de fondo: entrada, salida y mantenimiento | 5 | 572 |
| `core` | piezas comunes: errores, logs, métricas, trazas, resiliencia, reloj y texto | 10 | 697 |
| `cli` | línea de comandos: roles, `migrate`, `check-config`, `gen-secrets`, `key-usage`, `ingest` | 4 | 480 |
| raíz del paquete | `bootstrap.py` (armado de los componentes), `settings.py` (configuración validada) y el punto de entrada | 4 | 658 |

En total, 117 archivos y 11.675 líneas de Python. El más largo es el motor de respuesta
(`ai/engine.py`, 450 líneas). Las pruebas suman 51 archivos y 9.341 líneas, y el frontend unas
2.240 líneas de JavaScript.

## Reglas de dependencia

- **El núcleo no conoce la entrega.** `conversations`, `ai`, `rag` y `security` no importan la
  API, los workers ni las integraciones concretas: trabajan con contratos (protocolos de Python).
- **Las integraciones dependen del núcleo, nunca al revés.** Un adaptador conoce el modelo común
  de mensajes; el gestor de conversaciones no sabe de qué plataforma vino un mensaje.
- **Un solo lugar arma todo.** `bootstrap.py` es la raíz de composición: el único módulo que
  conoce las clases concretas. `integrations/factory.py` hace lo mismo con los canales. El resto
  recibe sus dependencias por constructor.
- **La persistencia implementa puertos.** Las interfaces que necesita el núcleo (auditoría,
  comandos de contacto, almacén de tokens) se definen junto a quien las usa y `persistence` las
  implementa con PostgreSQL.

## Principios SOLID en el código

| Principio | Dónde se ve |
|---|---|
| Responsabilidad única | cada rol hace una cosa (la API recibe, el worker responde, el dispatcher envía, el mantenimiento repara y limpia); cada adaptador separa verificar, interpretar, formatear y enviar; un repositorio por agregado en `persistence` |
| Abierto/cerrado | un canal nuevo se suma con un adaptador y una entrada en la fábrica, sin tocar el gestor de conversaciones, el motor ni los workers |
| Sustitución de Liskov | cualquier adaptador sirve donde se espera un `ChannelAdapter`; cualquier servidor compatible con la API de OpenAI sirve como `ChatModel` (vLLM en GPU, llama.cpp en CPU o el simulador de las pruebas) |
| Segregación de interfaces | contratos chicos y separados: `WebhookVerifier`, `WebhookParser`, `MessageFormatter`, `MessageSender` y `TypingNotifier`, este último opcional para los canales que no muestran "escribiendo" |
| Inversión de dependencias | el núcleo depende de `ChatModel`, `Embedder`, `Retriever`, `SemanticCache`, `JobQueue`, `TokenVerifier` y los puertos de `conversations/ports.py`; las clases concretas se inyectan desde `bootstrap.py` |

## Convenciones

- **Estilo y errores:** ruff con las reglas de estilo, imports, errores comunes, modernización,
  seguridad (bandit) y asincronía; líneas de hasta 120 caracteres. Sin observaciones en la
  versión entregada.
- **Tipos:** anotaciones en todo el código; datos de entrada validados con Pydantic, que además
  rechaza campos desconocidos en la API.
- **Asincronía:** todo el acceso a red y a la base es asincrónico (asyncpg, aio-pika, httpx), así
  que un proceso atiende muchos pedidos a la vez sin hilos.
- **Errores:** las capas internas lanzan errores del dominio (`core/errors.py`) y la API los
  traduce a respuestas HTTP en un solo lugar (`api/errors.py`), sin detalles internos.
- **Logs y métricas:** JSON con nombres de evento estables; métricas con prefijo `iamia_` y
  etiquetas acotadas (canal, resultado).
- **Textos:** los mensajes que ve la persona están juntos en `conversations/texts.py` y los
  prompts en `ai/prompts.py`, para revisarlos y versionarlos.
- **Configuración:** una sola clase (`settings.py`) lee las variables de entorno, las valida y
  revisa la seguridad de la combinación.

## Frontend

El chat web y la consola son páginas estáticas que sirve nginx, en JavaScript sin dependencias ni
paso de compilación. Arman la página con elementos del DOM y `textContent`, nunca con
`innerHTML`, y cumplen una política de contenido estricta (sin scripts en línea).

| Aplicación | Archivos | Qué hace cada uno |
|---|---|---|
| Chat web | `webchat.js`, `session.js`, `poller.js`, `view.js` | arranque, sesión firmada, espera de respuestas (long polling) y dibujo de la conversación |
| Consola | `console.js`, `api.js`, `conversations.js`, `detail.js`, `transcript.js`, `panels.js`, `ui.js` | arranque, llamadas a la API, lista, detalle, historial, paneles de canales, problemas y auditoría, y utilidades |

## Pruebas e infraestructura

- `backend/tests/unit` y `backend/tests/integration`: pruebas de la aplicación, con fábricas de
  datos de prueba compartidas (`factories.py`).
- `tests/e2e`: la plataforma completa en Compose, con simuladores de las 8 plataformas y del
  modelo (`simulators/`) y las suites por área (`tests/`).
- `tests/load`: el generador de carga y sus resultados.
- `infrastructure/kubernetes`: una `base` con todos los recursos (aplicación, PostgreSQL,
  RabbitMQ, Redis, modelos, ingress, oauth2-proxy, monitoreo y NetworkPolicy) con los valores
  de producción, y dos overlays: `production` fija la versión de la imagen y el emisor de
  certificados definitivo; `staging` achica réplicas y recursos y apunta a sus propios nombres,
  proveedor de identidad y secretos.
