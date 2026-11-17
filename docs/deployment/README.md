# Despliegue

Cómo instalar, actualizar, respaldar y apagar IAMiArgentina 1.1. Todas las formas usan la misma
imagen de la aplicación (`iamia/backend`) con un rol distinto por proceso.

| Forma | Para qué | Archivos |
|---|---|---|
| Compose, núcleo | desarrollo y pruebas de canales y consola, sin modelos | `docker-compose.yml` |
| Compose + modelos en CPU | demo con modelos reales (llama.cpp) en una PC o notebook | `+ compose.cpu.yml` |
| Compose + vLLM en GPU | un servidor de inferencia o un piloto en un solo equipo | `+ compose.gpu.yml` |
| Compose + observabilidad | Prometheus, Alertmanager, Grafana, Loki y trazas | `+ compose.observability.yml` |
| Kubernetes | producción con alta disponibilidad | [infrastructure/kubernetes](../../infrastructure/kubernetes/README.md) |

## Requisitos

- Docker Engine 24 o posterior (o Docker Desktop) con Compose v2.
- **Núcleo:** 2 vCPU, 4 GB de RAM, 10 GB de disco.
- **Modelos en CPU:** 6 núcleos y 16 GB de RAM (así se probó), más los archivos GGUF (Llama 3.2 3B
  Q4_K_M y bge-m3 Q8_0, unos 2,6 GB) en un volumen de Docker. Sirve para mostrar el sistema, no
  para atender gente: cada respuesta tarda entre 12 y 40 segundos.
- **GPU:** NVIDIA con 48 GB o más, driver y NVIDIA Container Toolkit. El dimensionamiento para
  producción está en el [plan de hardware](../hardware/README.md).
- En un servidor expuesto, TLS delante de nginx (balanceador o certificado propio): las
  plataformas solo mandan webhooks a direcciones HTTPS.

## Primera instalación con Compose

```bash
cp .env.example .env
docker compose build
docker compose run --rm --no-deps api gen-secrets     # pegar la salida en .env
docker compose run --rm --no-deps api check-config    # avisa qué falta o es inseguro
docker compose up -d
```

Las claves se generan **antes** del primer `up`: PostgreSQL, Redis y RabbitMQ fijan su contraseña
la primera vez que arrancan. Por eso el `--no-deps`, para que generar las claves no levante la base
con la contraseña de ejemplo. Con `APP_ENV=production`, `check-config` y el arranque de cada rol
cortan si falta una clave o queda una contraseña de ejemplo.

Solo nginx publica un puerto (`HTTP_PORT`, 8080 por defecto):

| Dirección | Qué es |
|---|---|
| `http://localhost:8080/` | chat web (redirige a `/chat/`) |
| `http://localhost:8080/console/` | consola de operadores |
| `http://localhost:8080/webhooks/<canal>` | webhooks de las plataformas |
| `http://localhost:8080/v1/chat` | API de consulta compatible con la v1 (nginx agrega `X-Edge-Auth`; límite por IP) |
| `http://localhost:8080/health/ready` | salud: base, Redis y cola |

### Con modelos

```bash
# En CPU (llama.cpp), con los GGUF ya descargados en el volumen LLAMA_MODELS_VOLUME:
docker compose -f docker-compose.yml -f compose.cpu.yml --profile models up -d

# En GPU (vLLM). La primera vez descarga los modelos al volumen hf-cache; sin salida a internet,
# precargar ese volumen desde el repositorio interno de modelos:
docker compose -f docker-compose.yml -f compose.gpu.yml --profile models up -d
```

El perfil `models` agrega la ingesta inicial (`ingest --if-empty`): indexa las FAQs de `data/faqs`
si la base de conocimiento está vacía. La imagen de vLLM está fijada por digest pero no se probó en
esta máquina, que no tiene GPU: validarla en el servidor antes de producción.

### Canales

Se habilitan con `CHANNELS_ENABLED` y las credenciales de cada plataforma en `.env`. Cómo obtener
cada credencial y registrar el webhook está en [docs/integrations](../integrations/README.md).
`check-config` avisa si un canal habilitado no tiene sus secretos.

### Consola

En producción la consola usa el proveedor de identidad de la organización (`OIDC_ISSUER`,
`OIDC_JWKS_URL`, `OIDC_AUDIENCE`). Para probarla en desarrollo, un par de claves local y un token:

```bash
mkdir -p .dev-keys
docker run --rm --user "$(id -u):$(id -g)" -e APP_ENV=development -v "$PWD/.dev-keys:/keys" \
  iamia/backend:1.1.0 dev-token --key-dir /keys --role operator
```

y un `docker-compose.override.yml` (no se versiona) que monte `./.dev-keys:/keys:ro` en `api` con
`OIDC_PUBLIC_KEY_PATH=/keys/oidc-dev-public.pem`. El token se pega en la consola y solo funciona
con `APP_ENV=development`. Roles y permisos: [manual de la consola](../manual/consola.md).

### Observabilidad

```bash
cp .env.observability.example .env.observability   # contraseña de Grafana, destinos de alertas
docker compose --env-file .env --env-file .env.observability \
  -f docker-compose.yml -f compose.observability.yml up -d
```

Suma Prometheus con las reglas de alerta, Alertmanager, Grafana con tres tableros (solo en
`127.0.0.1:3000`), Loki con Alloy para los logs, y el colector de OpenTelemetry con Tempo para las
trazas (`OTEL_TRACES_SAMPLE_RATIO` regula qué parte de los mensajes se traza). Detalle en
[infrastructure/monitoring](../../infrastructure/monitoring/README.md).

## Base de conocimiento

```bash
dc run --rm ingest ingest --if-changed     # vuelve a indexar si cambiaron las FAQs
dc run --rm ingest ingest --from-web       # suma las páginas oficiales de data/sources.txt
dc run --rm ingest ingest --dry-run        # muestra qué indexaría, sin escribir
dc run --rm ingest ingest --rollback       # vuelve a la versión anterior
```

(`dc` es el comando de compose de la instalación, con sus `-f` y `--profile models`.) Cada
ingesta arma una versión nueva aparte, la valida y recién entonces la activa: las consultas en
curso no se cortan y volver atrás es inmediato.

## Actualizar

```bash
git pull                     # o el tag de la versión nueva
docker compose build
docker compose up -d
```

`migrate` corre antes que los roles: la API y los workers arrancan cuando terminó bien. Las
migraciones se escriben compatibles con la versión anterior de la aplicación (primero se agrega,
en una versión posterior se quita lo que ya no se usa), así que para volver atrás alcanza con la
imagen anterior:

```bash
git checkout v1.1.0 && docker compose build && docker compose up -d
```

En Kubernetes el procedimiento (migración como Job, rollout por rol) está en
[infrastructure/kubernetes](../../infrastructure/kubernetes/README.md).

## Respaldos y recuperación

Lo único que hay que respaldar es PostgreSQL. RabbitMQ solo lleva identificadores (si se pierde,
el sweeper vuelve a publicar lo pendiente desde la base) y Redis es caché.

```bash
# Respaldo
docker compose exec -T postgres pg_dump -U iamia -Fc iamia > respaldo-$(date +%F).dump

# Recuperación (con los roles de la app detenidos)
docker compose stop api worker dispatcher maintenance
docker compose exec -T postgres pg_restore -U iamia -d iamia --clean --if-exists < respaldo-AAAA-MM-DD.dump
docker compose start api worker dispatcher maintenance
```

El respaldo tiene los datos personales cifrados: sin `DATA_ENCRYPTION_KEYS` y
`PSEUDONYMIZATION_KEY` no sirve, que es lo que se busca si alguien se lo lleva. Las claves se
guardan aparte, en el gestor de secretos, y se prueban junto con cada restauración.

En Kubernetes, CloudNativePG hace respaldos continuos (WAL) a almacenamiento de objetos con
recuperación a un punto en el tiempo; RPO, RTO y el sitio de contingencia están en el
[plan de hardware](../hardware/README.md#alta-disponibilidad-y-contingencia).

## Rotar claves

Procedimiento en [docs/security](../security/README.md#secretos). Para la clave de cifrado: la
nueva va primero en `DATA_ENCRYPTION_KEYS`, se despliega, el mantenimiento recifra lo viejo en
segundo plano y la anterior se retira cuando `python -m iamia key-usage` muestra que nada la usa.
En Kubernetes, la nueva se agrega primero al final y, en un segundo despliegue, se pasa al
principio (detalle en el mismo procedimiento).

## Verificar

```bash
curl -fsS http://localhost:8080/health/ready
scripts/e2e.sh        # en un entorno de prueba: plataforma completa con simuladores, 41 pruebas
```

## Apagar

```bash
docker compose down        # detiene todo y conserva los datos
docker compose down -v     # borra también los volúmenes: se pierde la base
```
