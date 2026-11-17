# Instalación paso a paso

## Formas de instalar

Todas las formas usan la misma imagen de la aplicación (`iamia/backend`), con un rol distinto
por proceso. Se elige según para qué se va a usar:

| Forma | Para qué | Archivos |
|---|---|---|
| Compose, núcleo | desarrollo y pruebas de canales y consola, sin modelos | `docker-compose.yml` |
| Compose con modelos en CPU | demostración con modelos reales (llama.cpp) en una PC | más `compose.cpu.yml` |
| Compose con vLLM en GPU | un servidor de inferencia o un piloto en un solo equipo | más `compose.gpu.yml` |
| Compose con observabilidad | métricas, alertas, tableros, logs y trazas | más `compose.observability.yml` |
| Kubernetes | producción con alta disponibilidad | `infrastructure/kubernetes` |

## Requisitos

| Caso | Requisitos |
|---|---|
| Todos | Docker Engine 24 o posterior (o Docker Desktop) con Compose v2; Git |
| Núcleo | 2 vCPU, 4 GB de RAM y 10 GB de disco |
| Modelos en CPU | 6 núcleos y 16 GB de RAM (así se probó) más los archivos GGUF de Llama 3.2 3B (Q4_K_M) y bge-m3 (Q8_0), unos 2,6 GB, en un volumen de Docker. Sirve para mostrar el sistema, no para atender gente: cada respuesta tarda entre 12 y 40 segundos |
| GPU | NVIDIA con 48 GB o más, driver y NVIDIA Container Toolkit |
| Servidor expuesto | TLS delante de nginx (balanceador o certificado propio): las plataformas solo mandan webhooks a direcciones HTTPS |

## Imágenes

| Servicio | Imagen | Origen |
|---|---|---|
| Aplicación (todos los roles) | `iamia/backend:1.1.0` | se construye desde `backend/Dockerfile` |
| PostgreSQL | `iamia/postgres:16-pgvector` | se construye: PostgreSQL 16 con pgvector |
| Redis | `redis:8.6-alpine` | oficial |
| RabbitMQ | `rabbitmq:4.3-management-alpine` | oficial |
| Borde | `nginx:1.30-alpine` | oficial |
| Modelos en CPU | servidor de llama.cpp | fijado por digest |
| Modelos en GPU | `vllm/vllm-openai:v0.33.0` | fijado por digest |

Las dependencias de Python están fijadas con versión exacta en `backend/requirements.txt`; la
imagen de la aplicación no tiene compiladores, pip, curl ni wget.

## Instalación con Docker Compose

**Paso 1. Obtener el código.**

```bash
git clone https://github.com/LucianoColalunga/IAMiArgentina-1.1.git
cd IAMiArgentina-1.1
```

**Paso 2. Crear la configuración** a partir del ejemplo, que no tiene valores secretos:

```bash
cp .env.example .env
```

**Paso 3. Construir las imágenes.**

```bash
docker compose build
```

**Paso 4. Generar las claves** y pegarlas en `.env`:

```bash
docker compose run --rm --no-deps api gen-secrets
```

Las claves se generan antes del primer arranque porque PostgreSQL, Redis y RabbitMQ fijan su
contraseña la primera vez que inician. El `--no-deps` evita que este paso levante la base con la
contraseña de ejemplo. Después de pegarlas, dejar el archivo legible solo para el usuario que
corre Docker (`chmod 600 .env`).

**Paso 5. Revisar la configuración.**

```bash
docker compose run --rm --no-deps api check-config
```

Avisa qué falta o es inseguro: claves sin definir, contraseñas de ejemplo, canales habilitados
sin sus secretos, consola sin OIDC. Con `APP_ENV=production` estos avisos impiden arrancar.

**Paso 6. Levantar la plataforma.**

```bash
docker compose up -d
docker compose ps
```

Primero corre `migrate`, que crea o actualiza el esquema de la base; la API, el worker, el
dispatcher y el mantenimiento arrancan cuando terminó bien. Solo nginx publica un puerto
(`HTTP_PORT`, 8080 por defecto):

| Dirección | Qué es |
|---|---|
| `http://localhost:8080/` | chat web |
| `http://localhost:8080/console/` | consola de operadores |
| `http://localhost:8080/webhooks/<canal>` | webhooks de las plataformas |
| `http://localhost:8080/v1/chat` | API de consulta compatible con la versión 1 |
| `http://localhost:8080/health/ready` | salud: base, Redis y cola |

## Verificar la instalación

```bash
# Salud: responde 200 si la base, Redis y RabbitMQ están disponibles
curl -fsS http://localhost:8080/health/ready

# Una consulta directa (necesita los modelos, ver más abajo)
curl -s http://localhost:8080/v1/chat -H "Content-Type: application/json" \
  -d '{"question": "¿Cómo saco turno para el DNI?"}'
```

El chat web se prueba abriendo `http://localhost:8080/` en el navegador. Sin modelos, la
plataforma recibe y guarda los mensajes, pero no puede generar respuestas: el worker reintenta
cada mensaje unas veces, con esperas crecientes, y si el modelo sigue sin responder le avisa a la
persona que el servicio no está disponible por el momento.

## Agregar los modelos

```bash
# En CPU (llama.cpp), con los archivos GGUF ya descargados en el volumen LLAMA_MODELS_VOLUME
docker compose -f docker-compose.yml -f compose.cpu.yml --profile models up -d

# En GPU (vLLM)
docker compose -f docker-compose.yml -f compose.gpu.yml --profile models up -d
```

El perfil `models` agrega la ingesta inicial (`ingest --if-empty`): si la base de conocimiento
está vacía, indexa las FAQs de `data/faqs`. En GPU, la primera vez se descargan los pesos al
volumen `hf-cache`; en un servidor sin salida a internet, ese volumen se precarga desde el
repositorio interno de modelos.

> La imagen de vLLM está fijada por digest pero no se probó en el equipo de desarrollo, que no
> tiene GPU. Validarla en el servidor antes de producción (capítulo 10).

## Probar la consola en desarrollo

En producción la consola usa el proveedor de identidad de la organización (`OIDC_ISSUER`,
`OIDC_JWKS_URL`, `OIDC_AUDIENCE`). Para probarla en un equipo de desarrollo se genera un par de
claves local y un token de prueba:

```bash
mkdir -p .dev-keys
docker run --rm --user "$(id -u):$(id -g)" -e APP_ENV=development \
  -v "$PWD/.dev-keys:/keys" iamia/backend:1.1.0 dev-token --key-dir /keys --role operator
```

Luego, un archivo `docker-compose.override.yml` (no se versiona) monta `./.dev-keys` en la API con
`OIDC_PUBLIC_KEY_PATH=/keys/oidc-dev-public.pem`. El token se pega en la consola y solo funciona
con `APP_ENV=development`.

## Observabilidad

```bash
cp .env.observability.example .env.observability
docker compose --env-file .env --env-file .env.observability \
  -f docker-compose.yml -f compose.observability.yml up -d
```

Suma Prometheus con las reglas de alerta, Alertmanager, Grafana con tres tableros (solo en
`127.0.0.1:3000`), Loki con Alloy para los logs, y el colector de OpenTelemetry con Tempo para
las trazas. El uso de cada uno está en el capítulo 9.

## Variables de entorno principales

| Variable | Para qué |
|---|---|
| `APP_ENV` | `development`, `staging` o `production`; en producción la configuración insegura impide arrancar |
| `LOG_LEVEL` | nivel de los logs (`INFO` por defecto) |
| `PUBLIC_BASE_URL` | dirección pública; con ella se reconstruye la URL que firman algunas plataformas (SMS) |
| `HTTP_PORT` | puerto que publica nginx (8080) |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `RABBITMQ_PASSWORD` | credenciales de los servicios internos |
| `DATA_ENCRYPTION_KEYS` | claves de cifrado `id:clave`, separadas por coma; la primera cifra |
| `PSEUDONYMIZATION_KEY` | clave del HMAC de las direcciones |
| `EDGE_SHARED_SECRET` | secreto que nginx agrega como `X-Edge-Auth` en `/v1/chat` |
| `LLM_BASE_URL`, `LLM_MODEL` | servidor y modelo de lenguaje |
| `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_MODEL`, `EMBEDDING_DIMENSIONS` | servidor, modelo y dimensión de los embeddings |
| `LOCAL_MODELS_API_KEY` | clave que exigen los servidores de modelos locales |
| `CHANNELS_ENABLED` | canales habilitados, separados por coma (por defecto, solo `webchat`) |
| `WEBCHAT_TOKEN_SECRET` | clave con que se firman las sesiones del chat web |
| `WHATSAPP_*`, `MESSENGER_*`, `INSTAGRAM_*`, `TELEGRAM_*`, `X_*`, `TWILIO_*`, `SMTP_*`, `EMAIL_*` | credenciales de cada canal (capítulo 3) |
| `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL` | proveedor de identidad de la consola |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_SAMPLE_RATIO` | exportación de trazas y qué parte de los mensajes se traza |

Los parámetros del motor (umbrales, cachés, tiempos) tienen valores por defecto probados y se
pueden cambiar con variables del mismo nombre; los principales están en el capítulo 4.

## Instalación en Kubernetes

Para producción, los manifiestos de `infrastructure/kubernetes` (kustomize, con overlays de
staging y producción) necesitan un clúster 1.31 o posterior con un CNI que aplique NetworkPolicy
y estos componentes: CloudNativePG con el plugin Barman Cloud, los operadores de RabbitMQ, KEDA,
External Secrets Operator, cert-manager, ingress-nginx, el GPU Operator de NVIDIA y
kube-prometheus-stack.

1. Instalar los operadores, el almacén de secretos, el emisor de certificados y las clases de
   almacenamiento.
2. Cargar los secretos en Vault.
3. Aplicar dependencias, red y configuración.
4. Descargar los modelos al volumen compartido con el Job `vllm-model-download`.
5. Correr la migración (Job `migrate`) y desplegar los roles de la aplicación.
6. Hacer la primera carga de la base de conocimiento desde el CronJob `ingest`.
7. Apuntar los nombres DNS públicos al balanceador del ingress.

Sin Argo CD, el pipeline aplica el manifiesto renderizado en tres pasos (dependencias, migración y
roles), porque kustomize no ordena los recursos:

```bash
kubectl kustomize infrastructure/kubernetes/overlays/production > iamia.yaml
# 1. Todo menos los roles de la aplicación y la migración (con selectores de etiquetas)
# 2. La migración: borrar el Job anterior, aplicar y esperar que termine
# 3. Los roles de la aplicación, y esperar el rollout
kubectl -n iamia rollout status deployment/api --timeout=10m
```

El detalle de cada paso, con los comandos completos, está en
`infrastructure/kubernetes/README.md`.

## Problemas comunes

| Síntoma | Causa habitual | Solución |
|---|---|---|
| `check-config` falla o un rol no arranca en producción | falta una clave o queda una contraseña de ejemplo | generar las claves con `gen-secrets` y completar `.env` |
| La base rechaza la contraseña después de cambiarla en `.env` | PostgreSQL fijó la contraseña la primera vez que arrancó | cambiarla también dentro de la base, o en una instalación nueva sin datos, `docker compose down -v` |
| El puerto 8080 está ocupado | otro programa lo usa | cambiar `HTTP_PORT` en `.env` |
| `/health/ready` responde 503 | PostgreSQL, Redis o RabbitMQ no están listos | `docker compose ps` y `docker compose logs <servicio>` |
| Los mensajes entran pero no hay respuestas | los modelos no están levantados o no responden | levantar con `--profile models` y revisar los logs del worker |
| Las respuestas tardan decenas de segundos | modelos en CPU | es lo esperable en CPU; para atender gente hace falta GPU |
| Un webhook responde 401 | el secreto de firma no coincide con el de la plataforma | revisar las variables del canal y la URL configurada (capítulo 3) |
| La plataforma no acepta la URL del webhook | la dirección no es HTTPS o no es pública | TLS delante de nginx y `PUBLIC_BASE_URL` con la dirección pública |
| La consola rechaza el token de prueba | el token de desarrollo solo funciona con `APP_ENV=development` | usar el proveedor de identidad en los demás entornos |
